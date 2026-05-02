import hassapi as hass

class GlobalSettings(hass.Hass):

    def initialize(self):
        """Called when the app is loaded."""
        self.log("GlobalSettings initialized.")

    def get_room_temp(self, room_alias):
        mapping = self.args.get("temp_room_map", {})
        return mapping.get(room_alias)

    def get_heating(self, key):
        mapping = self.args.get("heating_map", {})
        return mapping.get(key)

    def get_outdoor_temp(self, key="outdoor_temp"):
        """Used by RoomDemandCalculator for specific sensor lookups (like garten_temp)."""
        mapping = self.args.get("temp_outdoor_map", {})
        return mapping.get(key)

    def get_outdoor_sensor_hierarchy(self):
        """Used by HeatSupplyManager for dynamic failover of the main outdoor temp."""
        mapping = self.args.get("temp_outdoor_map", {})
        # Returns all sensors in the exact order they are listed in apps.yaml
        return list(mapping.values())

    def get_all_rooms(self):
        return self.args.get("temp_room_map", {})
    
    def get_valve_map(self, room_alias):
        """Returns the valve position sensor for a given room."""
        mapping = self.args.get("valve_map", {})
        # Note: Logic handles room names like 'stubbe' even if key is 'valve_stubbe'
        return mapping.get(f"valve_{room_alias}") or mapping.get(room_alias)


    def send_telegram(self, chat_id, title, message, disable_notification):
        """
        Centralized Telegram notification service.
        """
        self.call_service("telegram_bot/send_message", service_data={
            "chat_id": chat_id, # Match the variable name above
            "title": title,
            "message": message,
            "disable_notification": disable_notification
        })
