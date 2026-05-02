
import hassapi as hass  # type: ignore

# ==================================================================================================
# FROELING HEATING ESP INTERFACE
# ==================================================================================================
class FroelingHeatingESP(hass.Hass):
    def initialize(self):
        self.gl = self.get_app("global_config")
        self.telegram_chat_id = self.args.get('telegram_id')
        self.target_temp_helper = "input_number.target_flow_temp"
        self.modbus_sensor = "binary_sensor.froeling_esp_modbus_status"
        
        # Get the entity ID for the HK2 external pump control from globals
        self.hk2_pump_control = self.gl.get_heating("froeling_hk2_pump_external")
        
        self.startup_timer = None
        self.try_startup()

    def try_startup(self, kwargs=None):
        if not self.check_system_health():
            self.startup_timer = self.run_in(self.try_startup, 30)
            return
        self.boot_up()

    def check_system_health(self):
        if not self.entity_exists(self.target_temp_helper) or self.get_state(self.target_temp_helper) in ["unavailable", "unknown", None]:
            self.log(f"Waiting for {self.target_temp_helper}...", level="WARNING")
            return False
        return True

    def boot_up(self):
        self.log("Froeling ESP Interface Booted. Modbus watchdog active.")
        
        # Monitor Modbus health
        self.listen_state(self.on_modbus_status_change, self.modbus_sensor)
        
        # Monitor target temperature to trigger pump clearance
        self.listen_state(self.on_target_temp_change, self.target_temp_helper)
        
        # Initial check in case it's already down at boot
        if self.get_state(self.modbus_sensor) == "off":
            self.on_modbus_status_change(self.modbus_sensor, None, None, "off", None)
            
        # Run an initial check on the pump status
        self.check_and_enable_hk2()

    def on_target_temp_change(self, entity, attribute, old, new, kwargs):
        """Triggered whenever the target flow temperature is updated."""
        try:
            if float(new) > 0:
                self.check_and_enable_hk2()
        except (ValueError, TypeError):
            pass

    def check_and_enable_hk2(self):
        """Checks if HK2 is enabled and switches it on if heating is required."""
        if not self.hk2_pump_control:
            self.log("HK2 Pump Control entity not found in GlobalSettings.", level="ERROR")
            return

        current_state = self.get_state(self.hk2_pump_control)
        
        # Assuming 'on' or 'ON' is the required state for the select entity
        # Adjust 'ON' to the exact option string used by your Fröling Modbus integration
        if current_state.lower() != "on":
            self.log(f"Heating requested. Switching {self.hk2_pump_control} to On.", level="INFO")
            self.call_service("select/select_option", 
                              entity_id=self.hk2_pump_control, 
                              option="On")

    def on_modbus_status_change(self, entity, attribute, old, new, args):
        if new == "off":
            self.log("MODBUS DISCONNECTED!", level="ERROR")
            if self.telegram_chat_id:
                self.notify(self.telegram_chat_id, "🚨 Boiler Modbus Down", 
                            "ESP32 lost link to boiler.", True)
        elif new == "on" and old == "off":
            self.log("MODBUS RESTORED", level="INFO")
            if self.telegram_chat_id:
                self.notify(self.telegram_chat_id, "✅ Boiler Modbus Restored", 
                            "Modbus connection established.", True)

    def notify(self, chat_id, title, message, disable_notification=True):
        """Standardized Telegram call via GlobalSettings."""
        # Updated to use 'chat_id' keyword to match updated GlobalSettings definition
        self.gl.send_telegram(
            chat_id=chat_id,
            title=title,
            message=message,
            disable_notification=disable_notification
        )
