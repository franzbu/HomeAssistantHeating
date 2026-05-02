
import hassapi as hass  # type: ignore
from datetime import datetime, timedelta, time
import re

# ==================================================================================================
# ROOM DEMAND CALCULATOR
# ==================================================================================================
class RoomDemandCalculator(hass.Hass):     
    def initialize(self):
        self.gl = self.get_app("global_config")
        # Extract location from app name to allow code reuse across multiple rooms
        self.location = self.name.removeprefix("heating_") 
        self.sensor_temp = self.gl.get_room_temp(self.location)
        self.mode_mapping = {'Standard': 'standard', 'Holiday': 'holiday', 'Temporary': 'temp', 'Party': 'party', 'Google Calendar': 'calendar'}
        self.delay_timer = None
        self.calendar_cache = []
        self.calendar_entity = f"calendar.heating_{self.location}"
        self.calendar_timers = []
        self.calendar_sync_timer = None

        # Dynamically build schedule list to ensure the app reacts to all potential mode changes, excluding calendar
        self.my_schedules = [f'schedule.{s}_{self.location}' for s in list(self.mode_mapping.values()) if s != 'calendar']
        self.my_schedules.append(f'schedule.off_{self.location}')
        
        for sched in self.my_schedules:
            if self.entity_exists(sched):
                self.listen_state(self.callback_debounced_refresh, sched)
                self.listen_state(self.callback_debounced_refresh, sched, attribute='temp')
                self.listen_state(self.callback_debounced_refresh, sched, attribute='next_event')

        # Calendar Listeners
        if self.entity_exists(self.calendar_entity):
            # Listen to ALL attribute changes (catches newly added events or back-to-back updates)
            self.listen_state(self.on_calendar_state_change, self.calendar_entity, attribute="all")
            self.run_daily(self.update_calendar_cache, "03:00:00")
            
        self.listen_event(self.manual_calendar_refresh, "HEATING_CALENDAR_SYNC")

        self.listen_event(self.on_config_change, "entity_registry_updated")
        self.listen_state(self.callback_debounced_refresh, f'input_select.heating_schedule_{self.location}')
        self.listen_state(self.callback_temp_sensor, f'input_number.target_temp_{self.location}')
        self.listen_state(self.callback_debounced_refresh, f'input_number.delta_temp_{self.location}')
        self.listen_state(self.callback_temp_sensor, self.sensor_temp)

        # SUN COMPENSATION: Listen to Garten Temp and the room-specific helper
        self.sun_comp_helper = f"input_number.sun_compensation_{self.location}"
        self.garten_temp_sensor = self.gl.get_outdoor_temp("garten_temp")
        
        self.listen_state(self.callback_temp_sensor, self.garten_temp_sensor)
        if self.entity_exists(self.sun_comp_helper):
            self.listen_state(self.callback_temp_sensor, self.sun_comp_helper)
        
        # Listen to the Input Select for Master Off, auto, heating, party
        self.listen_state(self.callback_master_switch, "input_select.heating_mode")

        self.boost_helper = f"input_boolean.boost_enabled_{self.location}"
        if self.entity_exists(self.boost_helper):
            self.listen_state(self.callback_temp_sensor, self.boost_helper)
            self.listen_state(self.callback_temp_sensor, "input_number.heating_boost_threshold")
            self.listen_state(self.callback_temp_sensor, "input_number.heating_boost_factor")

        self.listen_event(self.force_refresh_handler, "HEATING_FORCE_EVALUATION")

        self.run_in(self.first_evaluation, 5)

    def force_refresh_handler(self, event_name, data, kwargs):
        entity = f'input_boolean.heating_claim_{self.location}'
        self.turn_off(entity)
        self.refresh_logic(force_reset=True)

    def on_config_change(self, event_name, data, kwargs):
        if data.get("entity_id", "") in self.my_schedules:
            self.prepare_dashboard_next_event()

    def callback_debounced_refresh(self, entity, attribute, old, new, args):
        self.update_dashboard_msg('Calculating next event...')
        if self.delay_timer:
            try: self.cancel_timer(self.delay_timer)
            except: pass
        
        # Use a longer debounce for schedule transitions to allow attributes to populate
        delay = 3 if (entity.startswith("schedule.") and new == "on") else 1
        self.delay_timer = self.run_in(self.first_evaluation, delay)

    def on_calendar_state_change(self, entity, attribute, old, new, args):
        """Triggers a fresh sync if a calendar event is dynamically added or changed."""
        if self.calendar_sync_timer:
            try: self.cancel_timer(self.calendar_sync_timer)
            except: pass
            
        # 1. Force the HA Template Sensors to update by "pressing" the button
        self.call_service("input_button/press", entity_id="input_button.refresh_calendars")
        
        # 2. Wait 5 seconds for HA to finish downloading before reading the cache
        self.calendar_sync_timer = self.run_in(self.delayed_calendar_sync, 5)

    def delayed_calendar_sync(self, kwargs):
        self.calendar_sync_timer = None
        self.update_calendar_cache()

    def update_calendar_cache(self, kwargs=None):
        # We now read the HA Template Sensors to bypass the AppDaemon API bug
        sensor_entity = f"sensor.calendar_events_{self.location}"
        
        if not self.entity_exists(sensor_entity):
            self.refresh_logic()
            self.prepare_dashboard_next_event() 
            return
        
        try:
            # Get the list of events from the sensor attribute we created in YAML
            events = self.get_state(sensor_entity, attribute="events")
            self.calendar_cache = events if isinstance(events, list) else []
            
            # Rebuild the internal timers for start/end of events
            self.setup_calendar_watchdogs()
            self.refresh_logic()
            self.prepare_dashboard_next_event() 
        except Exception as e:
            self.log(f"Calendar Sync Error: {e}", level="WARNING")
            self.update_dashboard_msg('Calendar sync failed.')

    def setup_calendar_watchdogs(self):
        """Creates precise internal triggers for the exact start/end moments of cached events."""
        for t in self.calendar_timers:
            try: self.cancel_timer(t)
            except: pass
        self.calendar_timers = []
        
        now = datetime.now().astimezone()
        for event in self.calendar_cache:
            try:
                start_dt = datetime.fromisoformat(event['start'].replace('Z', '+00:00')).astimezone()
                end_dt = datetime.fromisoformat(event['end'].replace('Z', '+00:00')).astimezone()
                
                if start_dt > now:
                    t = self.run_at(self.calendar_watchdog_trigger, start_dt + timedelta(seconds=1))
                    self.calendar_timers.append(t)
                if end_dt > now:
                    t = self.run_at(self.calendar_watchdog_trigger, end_dt + timedelta(seconds=1))
                    self.calendar_timers.append(t)
            except Exception:
                continue

    def calendar_watchdog_trigger(self, kwargs):
        self.refresh_logic()
        self.prepare_dashboard_next_event()

    def manual_calendar_refresh(self, event_name, data, kwargs):
        # Give HA's Template Sensor 5 seconds to fetch data from Google before reading it
        self.run_in(self.update_calendar_cache, 5)

    def get_active_calendar_event(self):
        now = datetime.now().astimezone()
        for event in self.calendar_cache:
            start = datetime.fromisoformat(event['start'].replace('Z', '+00:00')).astimezone()
            end = datetime.fromisoformat(event['end'].replace('Z', '+00:00')).astimezone()
            if start <= now <= end:
                return event
        return None

    def first_evaluation(self, kwargs):
        self.delay_timer = None
        
        mode = self.get_state(f'input_select.heating_schedule_{self.location}')
        if mode == "Google Calendar" and not self.calendar_cache:
            self.update_calendar_cache()
            return

        curr_sched = self.current_schedule()
        is_active = self.current_schedule_active()
        
        # Get both attributes to determine if the entity is fully loaded
        sched_temp = self.get_state(curr_sched, attribute='temp')
        next_event = self.get_state(curr_sched, attribute='next_event')

        # RACE CONDITION CHECK:
        # We only retry if the schedule is ON but BOTH attributes are missing.
        # If next_event exists but temp doesn't, it's a valid "No Temp" schedule.
        if is_active and mode != "Google Calendar" and (sched_temp is None) and (next_event is None or next_event == "None"):
            retry_count = kwargs.get("retry_count", 0)
            if retry_count < 2:
                self.log(f"⚠️ {curr_sched} is active but appears unloaded. Retrying in 5s...")
                self.run_in(self.first_evaluation, 5, retry_count=retry_count + 1)
                return

        # If we have data, or if it's a valid "No Temp" block, proceed to logic
        self.refresh_logic(force_reset=False)
        self.prepare_dashboard_next_event()

    def callback_master_switch(self, entity, attribute, old, new, args):
        force_start = (new in ["Heating", "OnFire"] and old not in ["Heating", "OnFire"])
        self.evaluate_heating_claim(force_start=force_start)

    def callback_temp_sensor(self, entity, attribute, old, new, args):
        self.evaluate_heating_claim() 

    def refresh_logic(self, force_reset=False):
        mode = self.get_state(f'input_select.heating_schedule_{self.location}')
        curr_sched = self.current_schedule()
        
        if curr_sched == f'schedule.off_{self.location}':
            target = 5.0
            self.set_target_temp(target)
            self.update_heating_claim(False)
            self.update_boost_attributes(0.0, 0.0, "off")
            self.update_sun_sensor(0.0)
            return 

        if mode == "Google Calendar":
            active_event = self.get_active_calendar_event()
            if active_event:
                summary = active_event.get('summary', '')
                try:
                    target = float(re.findall(r"[-+]?\d*\.\d+|\d+", summary)[0])
                except:
                    target = self.heat_temp()
            else:
                target = self.base_temp()
        elif self.current_schedule_active():
            sched_temp = self.get_state(curr_sched, attribute='temp')
            try: 
                target = float(sched_temp)
            except: 
                target = self.heat_temp() 
        else:
            target = self.base_temp()

        self.set_target_temp(target)
        
        # Calculate offset and update sensor once
        sun_offset = self.get_sun_offset()
        self.update_sun_sensor(sun_offset)
        
        effective_target = target - sun_offset
        self.evaluate_heating_claim(override_target=effective_target, force_reset=force_reset)

    def evaluate_heating_claim(self, override_target=None, force_reset=False, force_start=False):
        if self.get_state("input_select.heating_mode") in ["Off", "Pause"]:
            self.update_heating_claim(False)
            self.update_boost_attributes(0.0, 0.0, "off")
            self.update_sun_sensor(0.0)
            return

        if self.current_schedule() == f'schedule.off_{self.location}' or not self.current_schedule_active():
            self.update_heating_claim(False)
            self.update_boost_attributes(0.0, 0.0, "off")
            self.update_sun_sensor(0.0)
            return
            
        curr_t = self.current_temp()
        targ_t = override_target if override_target is not None else self.target_temp()
        if curr_t is None: return

        self.update_sun_sensor(self.get_sun_offset())

        current_state = self.get_state(f'input_boolean.heating_claim_{self.location}')
        has_claim = (current_state == 'on') if not force_reset else False
        
        upper_bound = targ_t - self.margin()
        lower_bound = targ_t - self.delta()

        if curr_t >= upper_bound:
            has_claim = False
        elif curr_t < lower_bound:
            has_claim = True
        elif force_start and curr_t < upper_bound:
            has_claim = True

        self.update_heating_claim(has_claim)
        self.calculate_and_update_boost(curr_t, targ_t)

    def calculate_and_update_boost(self, curr_t, targ_t):
        boost_enabled = "off"
        if self.entity_exists(f"input_boolean.boost_enabled_{self.location}"):
            boost_enabled = self.get_state(f"input_boolean.boost_enabled_{self.location}")
        
        factor = float(self.get_state("input_number.heating_boost_factor") or 1.0)
        threshold = float(self.get_state("input_number.heating_boost_threshold") or 4.0)
        
        raw_boost = 0.0
        if boost_enabled == "on" and (targ_t - curr_t) >= threshold:
            raw_boost = round(max(0.0, (targ_t - curr_t) * factor), 1)
        
        self.update_boost_attributes(raw_boost, raw_boost, boost_enabled)

    def update_boost_attributes(self, contribution, raw_boost, boost_enabled):
        ent_status = f"binary_sensor.boost_status_{self.location}"
        if boost_enabled is None or boost_enabled not in ["on", "off"]:
            boost_enabled = "off"
        
        try:
            is_active = float(contribution) > 0
        except:
            is_active = False

        status_state = "on" if is_active else "off"
        icon = "mdi:fire-alert" if is_active else "mdi:fire"
        if boost_enabled == "off":
            icon = "mdi:fire-off"
        
        self.set_state(ent_status, state=status_state, attributes={
            "friendly_name": f"Boost Status {self.location.capitalize()}",
            "boost": contribution if contribution is not None else 0.0,
            "raw_boost": raw_boost if raw_boost is not None else 0.0,
            "boost_enabled": str(boost_enabled),
            "icon": icon
        })

    def current_temp(self):
        try: 
            val = self.get_state(self.sensor_temp)
            return float(val) if val not in [None, "unavailable", "unknown"] else None
        except: return None

    # ==============================================================================================
    # SOLAR COMPENSATION LOGIC
    # ==============================================================================================
    def get_sun_offset(self):
        """Pure Query: Calculates offset based on greenhouse heat with baked-in limits."""
        # Check if the feature helper exists
        if not self.entity_exists(self.sun_comp_helper):
            return 0.0
            
        try:
            max_comp = float(self.get_state(self.sun_comp_helper) or 0)
            if max_comp == 0:
                return 0.0

            raw_g = self.get_state(self.garten_temp_sensor)
            if raw_g in [None, "unavailable", "unknown"]:
                return 0.0
            g_temp = float(raw_g)
            
            # --- BAKED IN VALUES ---
            start_t = 20.0
            peak_t = 35.0
            # -----------------------

            if g_temp <= start_t:
                factor = 0.0
            elif g_temp >= peak_t:
                factor = 1.0
            else:
                denom = peak_t - start_t
                factor = (g_temp - start_t) / denom if denom != 0 else 0.0

            return round(factor * max_comp, 2)

        except Exception as e:
            self.log(f"Solar Calc Error: {e}", level="WARNING")
            return 0.0

    def update_sun_sensor(self, offset):
        """Command: Updates the HA binary sensor."""
        # Kept as binary_sensor to match boost_status behavior
        ent_sun = f"binary_sensor.sun_compensation_{self.location}"
        
        is_active = offset > 0
        g_val = self.get_state(self.garten_temp_sensor)
        
        # State is on/off, but the specific delta is available in the 'compensation' attribute
        self.set_state(ent_sun, state="on" if is_active else "off", attributes={
            "friendly_name": f"Sun Compensation {self.location.capitalize()}",
            "compensation": offset,
            "garten_temp": g_val,
            "icon": "mdi:weather-sunny-alert" if is_active else "mdi:weather-sunny"
        })

    def target_temp(self):
        """Pure Query: Returns current target minus calculated sun offset."""
        try: 
            val = float(self.get_state(f'input_number.target_temp_{self.location}'))
            return val - self.get_sun_offset()
        except: return 5.0

    def base_temp(self):
        try: return float(self.get_state(f'input_number.base_temp_{self.location}'))
        except: return 5.0

    def delta(self):
        try: return float(self.get_state(f'input_number.delta_temp_{self.location}'))
        except: return 2

    def margin(self):
        try: return float(self.get_state('input_number.heating_margin'))
        except: return 0.5

    def heat_temp(self):
        try: return float(self.get_state(f'input_number.heat_temp_{self.location}'))
        except: return 21.0

    def current_schedule(self):
        mode = self.get_state(f'input_select.heating_schedule_{self.location}')
        suffix = self.mode_mapping.get(mode, 'off')
        return f'schedule.{suffix}_{self.location}'
    
    def current_schedule_active(self):
        mode = self.get_state(f'input_select.heating_schedule_{self.location}')
        if mode == "Google Calendar":
            return self.get_active_calendar_event() is not None
        return self.get_state(self.current_schedule()) == 'on'

    def set_target_temp(self, x):
        try:
            if float(self.get_state(f'input_number.target_temp_{self.location}')) == x: return
        except: pass
        self.call_service("input_number/set_value", entity_id=f'input_number.target_temp_{self.location}', value=x)

    def update_heating_claim(self, has_claim):
        entity = f'input_boolean.heating_claim_{self.location}'
        new_state = 'on' if has_claim else 'off'
        if self.get_state(entity) != new_state:
            self.turn_on(entity) if has_claim else self.turn_off(entity)

    def update_dashboard_msg(self, msg):
        self.call_service("input_text/set_value", entity_id=f'input_text.next_event_{self.location}', value=msg)

    def prepare_dashboard_next_event(self):
        mode = self.get_state(f'input_select.heating_schedule_{self.location}')
        if mode == "Google Calendar":
            self.calculate_calendar_relay_chain()
        else:
            curr_sched = self.current_schedule()
            self.last_schedule_response = self.call_service("schedule/get_schedule", entity_id=curr_sched)
            self.run_in(self.calculate_relay_chain, 1, sched_entity=curr_sched)

    def calculate_calendar_relay_chain(self):
        if not self.calendar_cache:
            self.update_dashboard_msg('No calendar data cached.')
            return

        now = datetime.now().astimezone()
        active_event = self.get_active_calendar_event()

        if active_event:
            current_end = datetime.fromisoformat(active_event['end'].replace('Z', '+00:00')).astimezone()
            limit_dt = now + timedelta(days=7)
            while current_end < limit_dt:
                found_next = False
                for event in self.calendar_cache:
                    evt_start = datetime.fromisoformat(event['start'].replace('Z', '+00:00')).astimezone()
                    if abs((evt_start - current_end).total_seconds()) <= 65:
                        current_end = datetime.fromisoformat(event['end'].replace('Z', '+00:00')).astimezone()
                        found_next = True
                        break
                if not found_next:
                    break
            msg = f"Heating stops at {self.format_time_msg(current_end)}"
        else:
            upcoming = []
            for event in self.calendar_cache:
                evt_start = datetime.fromisoformat(event['start'].replace('Z', '+00:00')).astimezone()
                if evt_start > now:
                    upcoming.append(evt_start)
            
            if upcoming:
                next_start = min(upcoming)
                msg = f"Heating starts at {self.format_time_msg(next_start)}"
            else:
                msg = "No upcoming events found."

        self.update_dashboard_msg(msg)

    def calculate_relay_chain(self, kwargs):
        if self.delay_timer is not None:
            return
        curr_sched = kwargs["sched_entity"]
        try:
            full_data = self.last_schedule_response
            res_obj = full_data.get("result", {}).get("response", {})
            rules_dict = res_obj.get(curr_sched, {})
            next_event_str = self.get_state(curr_sched, attribute='next_event')
            if not rules_dict or not next_event_str or next_event_str == "None":
                self.update_dashboard_msg('No heating scheduled.')
                return
            event_dt = datetime.strptime(next_event_str, '%Y-%m-%dT%H:%M:%S%z').astimezone()
            if self.current_schedule_active():
                true_end_dt = self.find_true_chain_end(event_dt, rules_dict)
                limit_dt = event_dt + timedelta(days=7)
                msg = 'Heating stops at next power cut ;)' if true_end_dt >= limit_dt else f"Heating stops at {self.format_time_msg(true_end_dt)}"
            else:
                msg = f"Heating starts at {self.format_time_msg(event_dt)}"
            self.update_dashboard_msg(msg)
        except Exception as e:
            self.log(f"Response Evaluation Error: {e}")

    def find_true_chain_end(self, start_dt, rules_dict):
        current_dt = start_dt
        limit_dt = start_dt + timedelta(days=7)
        while current_dt < limit_dt:
            day_name = current_dt.strftime('%A').lower()
            day_rules = rules_dict.get(day_name, [])
            found_link = False
            for block in day_rules:
                try:
                    f_str = block.get('from')
                    f_time = datetime.strptime(f_str, '%H:%M:%S' if len(f_str) > 5 else '%H:%M').time()
                    block_start_dt = datetime.combine(current_dt.date(), f_time).replace(tzinfo=current_dt.tzinfo)
                    if abs((block_start_dt - current_dt).total_seconds()) <= 65:
                        to_str = block.get('to')
                        if ".999999" in to_str:
                            current_dt = (current_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                        else:
                            t_parts = [int(p) for p in to_str.split(':')]
                            current_dt = current_dt.replace(hour=t_parts[0], minute=t_parts[1], second=t_parts[2] if len(t_parts) > 2 else 0, microsecond=0)
                        found_link = True
                        break 
                except: continue
            if not found_link: break
        return current_dt

    def format_time_msg(self, date_obj):
        now = datetime.now(date_obj.tzinfo)
        if date_obj.date() == now.date(): return date_obj.strftime('%H:%M.')
        elif date_obj.date() == (now + timedelta(days=1)).date(): return date_obj.strftime('%H:%M tomorrow.')
        else: return date_obj.strftime('%H:%M on %d.%m.')
        
# ==================================================================================================
# HEAT SUPPLY MANAGER
# ==================================================================================================
class HeatSupplyManager(hass.Hass):
    def initialize(self):
        # PHASE 1: Static Initialization (Runs ONCE)
        self.startup_time = datetime.now()
        self.stabilization_scheduled = False
        self.gl = self.get_app("global_config")
        raw_deps = self.args.get('dependencies', [])
        self.managed_locations = [d.replace("heating_", "") for d in raw_deps if d not in ["global_config", "heat_supply_manager"]]
        
        self.valve_map = self.gl.args.get("valve_map", {})
        self.flow_target_helper = "input_number.target_flow_temp"     
        self.mode_select = "input_select.heating_mode"

        self.ext_temp_sensors = self.gl.get_outdoor_sensor_hierarchy()
        self.telegram_chat_id = self.args.get('telegram_id') 
        
        self.debounce_timer = None
        self.claim_start_times = {} 
        self.startup_timer = None

        self.fallback_timer = None
        self.run_daily(self.reset_pause, time(0, 0, 0))
        self.run_daily(self.reset_schedules_to_default, time(0, 0, 0))

        # Start the health check loop
        self.try_startup()

    def try_startup(self, kwargs=None):
        # PHASE 2: Health Check Loop
        if not self.check_system_health():
            self.startup_timer = self.run_in(self.try_startup, 30)
            return

        # PHASE 3: Boot (Only reached when healthy)
        self.boot_up()

    def check_system_health(self):
        critical_entities = [
            self.flow_target_helper,
            self.mode_select
        ]
        
        missing = []
        unavailable = []
        
        for entity in critical_entities:
            if not self.entity_exists(entity):
                missing.append(entity)
                continue
            
            state = self.get_state(entity)
            if state in ["unavailable", "unknown", None]:
                unavailable.append(f"{entity} ({state})")

        # Check temperature sensor hierarchy separately
        temp_sensor_healthy = False
        for sensor in self.ext_temp_sensors:
            if self.entity_exists(sensor):
                state = self.get_state(sensor)
                if state not in ["unavailable", "unknown", None]:
                    temp_sensor_healthy = True
                    break
                    
        if not temp_sensor_healthy:
            unavailable.append("Any valid outdoor temp sensor")

        if missing:
            self.log(f"CRITICAL: Entities missing: {missing}", level="ERROR")
            return False

        if unavailable:
            self.log(f"Startup delayed. Waiting for: {unavailable}", level="WARNING")
            return False
        
        return True

    def boot_up(self):
        self.log("System Healthy. Registering listeners.")
        
        for loc in self.managed_locations:
            self.listen_state(self.callback_debounced_eval, f"input_boolean.heating_claim_{loc}")
            status_sensor = f"binary_sensor.boost_status_{loc}"
            self.listen_state(self.callback_debounced_eval, status_sensor, attribute="all")
            
        for sensor in self.ext_temp_sensors:
            self.listen_state(self.callback_debounced_eval, sensor)
            
        self.listen_state(self.on_mode_change, self.mode_select)
        
        config_entities = [
            "input_number.heating_baseline_0_deg",
            "input_number.baseline_adjustment",
            "input_number.max_flow_temp",
            "input_number.heating_boost_factor",
            "input_number.heating_boost_threshold",
            "input_number.heating_claim_duration",
            "input_number.flow_temp_multi_room_offset" 
        ]
        for e in config_entities:
            self.listen_state(self.callback_debounced_eval, e)
                
        self.evaluate_heating_pump()

    def _set_flow_target(self, new_val):
        """Internal helper: Update Home Assistant only if value changed."""
        try:
            current = float(self.get_state(self.flow_target_helper) or -1.0)
        except (ValueError, TypeError):
            current = -1.0
        if new_val != current:
            self.call_service("input_number/set_value", entity_id=self.flow_target_helper, value=new_val)
    
    def on_mode_change(self, entity, attribute, old, new, args):
        # Update our persistent timestamp helper to survive HA reboots
        if self.entity_exists("input_datetime.heating_mode_last_changed"):
            now_ts = datetime.now().timestamp()
            self.call_service("input_datetime/set_datetime", entity_id="input_datetime.heating_mode_last_changed", timestamp=now_ts)

        if new in ["Off", "Pause"]:
            self._set_flow_target(0)
            return
        if old == "Heating" and new == "Auto":
            self.reset_all_claims()
        self.callback_debounced_eval(entity, attribute, old, new, args)

    def reset_all_claims(self):
        for loc in self.managed_locations:
            claim = f"input_boolean.heating_claim_{loc}"
            if self.get_state(claim) == "on":
                self.turn_off(claim)

    def callback_debounced_eval(self, entity, attribute, old, new, args):
        if self.debounce_timer:
            try: self.cancel_timer(self.debounce_timer)
            except: pass 
        self.debounce_timer = self.run_in(self.retry_evaluation, 3)

    def retry_evaluation(self, kwargs):
        self.debounce_timer = None
        self.evaluate_heating_pump()

    def reset_pause(self, kwargs):
        if self.get_state(self.mode_select) == "Pause":
            self.call_service("input_select/select_option", entity_id=self.mode_select, option="Auto")

    def reset_schedules_to_default(self, kwargs):
        # 1. Abort the midnight reset if the system is currently OnFire
        if self.get_state(self.mode_select) == "OnFire":
            self.log("Midnight reset skipped: System is currently OnFire.", level="INFO")
            return

        for loc in self.managed_locations:
            schedule_entity = f"input_select.heating_schedule_{loc}"
            default_entity = f"input_select.default_heating_schedule_{loc}"
            
            if self.entity_exists(default_entity) and self.entity_exists(schedule_entity):
                default_val = self.get_state(default_entity)
                current_val = self.get_state(schedule_entity)
                
                # 2. Only fire the service call if the state actually needs to change
                if default_val and current_val != default_val:
                    self.call_service("input_select/select_option", entity_id=schedule_entity, option=default_val)

    def execute_mode_fallback(self, kwargs):
        self.fallback_timer = None
        fallback_mode = kwargs.get("fallback_mode")
        
        # Verify mode hasn't changed manually while we were waiting
        if self.get_state(self.mode_select) == fallback_mode:
            self.call_service("input_select/select_option", entity_id=self.mode_select, option="Auto")
            if fallback_mode == "Party" and self.telegram_chat_id:
                self.notify(self.telegram_chat_id, "🛑 Party Mode Ended", "Valves remained closed (< 15%).", True)

    def evaluate_heating_pump(self):
        # Prevent spike on HA restart by giving RoomDemandCalculator 10s to clear stale restored claims
        time_since_boot = (datetime.now() - self.startup_time).total_seconds()
        if time_since_boot < 10:
            self._set_flow_target(0)
            if not self.stabilization_scheduled:
                self.stabilization_scheduled = True
                self.run_in(self.retry_evaluation, int(11 - time_since_boot))
            return

        mode = self.get_state(self.mode_select)

        # --- SAFETY NET FOR PAUSE MODE ---
        if mode == "Pause":
            last_updated = None
            
            # 1. Try persistent helper first (survives HA reboots)
            if self.entity_exists("input_datetime.heating_mode_last_changed"):
                timestamp_str = self.get_state("input_datetime.heating_mode_last_changed")
                if timestamp_str and timestamp_str not in ["unknown", "unavailable"]:
                    try:
                        # input_datetime format is typically 'YYYY-MM-DD HH:MM:SS'
                        dt_naive = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                        # Localize it to current timezone
                        last_updated = dt_naive.astimezone()
                    except Exception as e:
                        self.log(f"Error parsing input_datetime: {e}", level="WARNING")

            # 2. Fallback to HA state attribute (vulnerable to reboots)
            if not last_updated:
                state_obj = self.get_state(self.mode_select, attribute="all")
                if state_obj and state_obj.get("last_changed"):
                    try:
                        last_updated = datetime.fromisoformat(state_obj["last_changed"].replace("Z", "+00:00")).astimezone()
                    except Exception as e:
                        self.log(f"Pause safety net error: {e}", level="WARNING")
            
            # Evaluate against current date
            if last_updated:
                if last_updated.date() < datetime.now().astimezone().date():
                    self.call_service("input_select/select_option", entity_id=self.mode_select, option="Auto")
                    return # Stop here, the state change will trigger a fresh evaluation

        if mode in ["Off", "Pause"]: 
            self._set_flow_target(0)
            if self.fallback_timer:
                self.cancel_timer(self.fallback_timer)
                self.fallback_timer = None
            return

        now = datetime.now()
        user_duration = int(float(self.get_state("input_number.heating_claim_duration") or 10))
        
        for loc in self.managed_locations:
            if self.get_state(f"input_boolean.heating_claim_{loc}") == 'on':
                if loc not in self.claim_start_times: self.claim_start_times[loc] = now 
            else:
                self.claim_start_times.pop(loc, None)

        active_claims = [loc for loc, start in self.claim_start_times.items() 
                         if (now - start).total_seconds() >= user_duration]

        should_heat = False
        party_fallback_met = False
        heating_fallback_met = False
        
        if mode == "Party":
            max_valve = 0.0
            found_valves = False
            for loc in self.managed_locations:
                valve_entity = self.valve_map.get(f"valve_{loc}")
                if valve_entity and self.entity_exists(valve_entity):
                    found_valves = True
                    try:
                        val = float(self.get_state(valve_entity) or 0)
                        if val > max_valve: max_valve = val
                    except: pass
            if found_valves and max_valve < 15.0:
                party_fallback_met = True
            else:
                should_heat = True

        elif active_claims:
            should_heat = True
        else:
            if mode == "Heating":
                heating_fallback_met = True

        # Handle the timers for automatic fallbacks
        if party_fallback_met:
            state_obj = self.get_state(self.mode_select, attribute="all")
            party_duration = 0
            if state_obj and state_obj.get("last_changed"):
                try:
                    last_changed_dt = datetime.fromisoformat(state_obj["last_changed"].replace("Z", "+00:00")).astimezone()
                    party_duration = (datetime.now().astimezone() - last_changed_dt).total_seconds()
                except Exception:
                    pass
            
            delay = 0 if party_duration > 60 else max(0, int(60 - party_duration))
            
            if self.fallback_timer is None:
                if delay == 0:
                    self.execute_mode_fallback({"fallback_mode": mode})
                else:
                    self.fallback_timer = self.run_in(self.execute_mode_fallback, delay, fallback_mode=mode)
                    
        elif heating_fallback_met:
            if self.fallback_timer is None:
                self.fallback_timer = self.run_in(self.execute_mode_fallback, 30, fallback_mode=mode)
                
        else:
            if self.fallback_timer is not None:
                self.cancel_timer(self.fallback_timer)
                self.fallback_timer = None

        if not should_heat:
            self._set_flow_target(0)
            return

        out_t = 0.0
        for sensor in self.ext_temp_sensors:
            raw_out = self.get_state(sensor)
            if raw_out not in [None, "unavailable", "unknown"]:
                try:
                    out_t = float(raw_out)
                    break
                except ValueError:
                    pass
                    
        adj_factor = float(self.get_state("input_number.baseline_adjustment") or 0.4)
        baseline = (-adj_factor * out_t) + float(self.get_state("input_number.heating_baseline_0_deg") or 36.0)
        
        max_realized_boost = 0.0
        for loc in active_claims:
            realized = float(self.get_state(f"binary_sensor.boost_status_{loc}", attribute="boost") or 0.0)
            if realized > max_realized_boost:
                max_realized_boost = realized

        multi_room_factor = float(self.get_state("input_number.flow_temp_multi_room_offset") or 0.0)
        multi_room_boost = max(0, len(active_claims) - 1) * multi_room_factor

        calc_target = float(round((baseline + max_realized_boost + multi_room_boost) * 2) / 2)
        max_f = float(self.get_state("input_number.max_flow_temp") or 45.0)
        if calc_target > max_f: calc_target = max_f
            
        self._set_flow_target(calc_target)
        if mode not in ["Heating", "Party", "OnFire"]:
            self.call_service("input_select/select_option", entity_id=self.mode_select, option="Heating")

    def notify(self, chat_id, title, message, disable_notification=True):
        """Standardized Telegram call via GlobalSettings."""
        # Updated call to use 'chat_id' keyword to match your updated GlobalSettings definition
        self.gl.send_telegram(
            chat_id=chat_id,
            title=title,
            message=message,
            disable_notification=disable_notification
        )
