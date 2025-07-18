# G-Code G1 movement commands (and associated coordinate manipulation)
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import os,json
from .base_info import base_dir, system_info_instance
class GCodeMove:
    def __init__(self, config):
        self.config = config
        self.variable_safe_z = 0
        if config.has_section('gcode_macro xyz_ready'):
            PRINTER_PARAM = config.getsection('gcode_macro xyz_ready')
            self.variable_safe_z = PRINTER_PARAM.getfloat('variable_safe_z', 0.0)
        self.printer = printer = config.get_printer()
        printer.register_event_handler("klippy:ready", self._handle_ready)
        printer.register_event_handler("klippy:shutdown", self._handle_shutdown)
        printer.register_event_handler("toolhead:set_position",
                                       self.reset_last_position)
        printer.register_event_handler("toolhead:manual_move",
                                       self.reset_last_position)
        printer.register_event_handler("gcode:command_error",
                                       self.reset_last_position)
        printer.register_event_handler("extruder:activate_extruder",
                                       self._handle_activate_extruder)
        printer.register_event_handler("homing:home_rails_end",
                                       self._handle_home_rails_end)
        self.is_printer_ready = False
        # Register g-code commands
        gcode = printer.lookup_object('gcode')
        handlers = [
            'G1', 'G20', 'G21',
            'M82', 'M83', 'G90', 'G91', 'G92', 'M220', 'M221',
            'SET_GCODE_OFFSET', 'SAVE_GCODE_STATE', 'RESTORE_GCODE_STATE',
        ]
        for cmd in handlers:
            func = getattr(self, 'cmd_' + cmd)
            desc = getattr(self, 'cmd_' + cmd + '_help', None)
            gcode.register_command(cmd, func, False, desc)
        gcode.register_command('G0', self.cmd_G1)
        gcode.register_command('M114', self.cmd_M114, True)
        gcode.register_command('GET_POSITION', self.cmd_GET_POSITION, True,
                               desc=self.cmd_GET_POSITION_help)
        gcode.register_command('SET_POSITION', self.cmd_SET_POSITION, True, desc=self.cmd_SET_POSITION_help)
        self.Coord = gcode.Coord
        # G-Code coordinate manipulation
        self.absolute_coord = self.absolute_extrude = True
        self.base_position = [0.0, 0.0, 0.0, 0.0]
        self.last_position = [0.0, 0.0, 0.0, 0.0]
        self.homing_position = [0.0, 0.0, 0.0, 0.0]
        self.speed = 25.
        self.speed_factor = 1. / 60.
        self.extrude_factor = 1.
        # G-Code state
        self.saved_states = {}
        self.move_transform = self.move_with_transform = None
        self.position_with_transform = (lambda: [0., 0., 0., 0.])
        gcode.register_command('SET_LIMITS', self.cmd_set_limits, desc=self.cmd_SET_LIMITS_help)
        gcode.register_command('RESTORE_LIMITS', self.cmd_restore_limits, desc=self.cmd_RESTORE_LIMITS_help)
        self.absolute_extrude_flag_path = os.path.join(base_dir, "creality/userdata/config/absolute_extrude_flag.json")
		
    cmd_SET_LIMITS_help = ("SET NEW LIMITS MAXY 400")
    def cmd_set_limits(self, gcmd):
        min_x = self.config.getsection('stepper_x').getfloat('position_min', default=-19)
        max_x = self.config.getsection('stepper_x').getfloat('position_max', default=285)
        min_y = self.config.getsection('stepper_y').getfloat('position_min', default=-7)
        max_y = self.config.getsection('stepper_y').getfloat('position_max', default=273)
        self.printer.lookup_object('toolhead').kin.limits[0] = (min_x,max_x)
        self.printer.lookup_object('toolhead').kin.limits[1] = (min_y,max_y)
		
    cmd_RESTORE_LIMITS_help = ("RESTORE_LIMITS")
    def cmd_restore_limits(self, gcmd):
        for i, rail in enumerate(self.printer.lookup_object('toolhead').kin.rails):
            if i==0 or i==1:
                self.printer.lookup_object('toolhead').kin.limits[i] = rail.get_range()
			
    def _handle_ready(self):
        self.is_printer_ready = True
        if self.move_transform is None:
            toolhead = self.printer.lookup_object('toolhead')
            self.move_with_transform = toolhead.move
            self.position_with_transform = toolhead.get_position
        self.reset_last_position()
    def _handle_shutdown(self):
        if not self.is_printer_ready:
            return
        self.is_printer_ready = False
        logging.info("gcode state: absolute_coord=%s absolute_extrude=%s"
                     " base_position=%s last_position=%s homing_position=%s"
                     " speed_factor=%s extrude_factor=%s speed=%s",
                     self.absolute_coord, self.absolute_extrude,
                     self.base_position, self.last_position,
                     self.homing_position, self.speed_factor,
                     self.extrude_factor, self.speed)
    def _handle_activate_extruder(self):
        self.reset_last_position()
        self.extrude_factor = 1.
        self.base_position[3] = self.last_position[3]
    def _handle_home_rails_end(self, homing_state, rails):
        self.reset_last_position()
        for axis in homing_state.get_axes():
            self.base_position[axis] = self.homing_position[axis]
    def set_move_transform(self, transform, force=False):
        if self.move_transform is not None and not force:
            raise self.printer.config_error(
                "G-Code move transform already specified")
        old_transform = self.move_transform
        if old_transform is None:
            old_transform = self.printer.lookup_object('toolhead', None)
        self.move_transform = transform
        self.move_with_transform = transform.move
        self.position_with_transform = transform.get_position
        return old_transform
    def _get_gcode_position(self):
        p = [lp - bp for lp, bp in zip(self.last_position, self.base_position)]
        p[3] /= self.extrude_factor
        return p
    def _get_gcode_speed(self):
        return self.speed / self.speed_factor
    def _get_gcode_speed_override(self):
        return self.speed_factor * 60.
    def get_status(self, eventtime=None):
        move_position = self._get_gcode_position()
        return {
            'speed_factor': self._get_gcode_speed_override(),
            'speed': self._get_gcode_speed(),
            'extrude_factor': self.extrude_factor,
            'absolute_coordinates': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'homing_origin': self.Coord(*self.homing_position),
            'position': self.Coord(*self.last_position),
            'gcode_position': self.Coord(*move_position),
        }
    def reset_last_position(self):
        if self.is_printer_ready:
            self.last_position = self.position_with_transform()
    # G-Code movement commands
    def cmd_G1(self, gcmd):
        # Move
        params = gcmd.get_command_parameters()
        try:
            for pos, axis in enumerate('XYZ'):
                if axis in params:
                    v = float(params[axis])
                    if not self.absolute_coord:
                        # value relative to position of last move
                        self.last_position[pos] += v
                    else:
                        # value relative to base coordinate position
                        self.last_position[pos] = v + self.base_position[pos]
            if 'E' in params:
                v = float(params['E']) * self.extrude_factor
                if not self.absolute_coord or not self.absolute_extrude:
                    # value relative to position of last move
                    self.last_position[3] += v
                else:
                    # value relative to base coordinate position
                    self.last_position[3] = v + self.base_position[3]
            if 'F' in params:
                gcode_speed = float(params['F'])
                if gcode_speed <= 0.:
                    raise gcmd.error("""{"code":"key272": "msg":"Invalid speed in '%s'", "values":["%s"]}"""
                                     % (gcmd.get_commandline(),gcmd.get_commandline()))
                self.speed = gcode_speed * self.speed_factor
        except ValueError as e:
            raise gcmd.error("""{"code":"key273": "msg":"Unable to parse move '%s'", "values":["%s"]}"""
                             % (gcmd.get_commandline(),gcmd.get_commandline()))
        self.move_with_transform(self.last_position, self.speed)
    # G-Code coordinate manipulation
    def cmd_G20(self, gcmd):
        # Set units to inches
        raise gcmd.error('Machine does not support G20 (inches) command')
    def cmd_G21(self, gcmd):
        # Set units to millimeters
        pass
    def cmd_M82(self, gcmd):
        # Use absolute distances for extrusion
        self.absolute_extrude = True
        with open(self.absolute_extrude_flag_path, "w") as f:
            f.write(json.dumps({"absolute_extrude_flag": 1}))
            f.flush()
    def cmd_M83(self, gcmd):
        # Use relative distances for extrusion
        self.absolute_extrude = False
        with open(self.absolute_extrude_flag_path, "w") as f:
            f.write(json.dumps({"absolute_extrude_flag": 0}))
            f.flush()
    def cmd_G90(self, gcmd):
        # Use absolute coordinates
        self.absolute_coord = True
    def cmd_G91(self, gcmd):
        # Use relative coordinates
        self.absolute_coord = False
    def cmd_G92(self, gcmd):
        # Set position
        offsets = [ gcmd.get_float(a, None) for a in 'XYZE' ]
        for i, offset in enumerate(offsets):
            if offset is not None:
                if i == 3:
                    offset *= self.extrude_factor
                self.base_position[i] = self.last_position[i] - offset
        if offsets == [None, None, None, None]:
            self.base_position = list(self.last_position)
    def cmd_M114(self, gcmd):
        # Get Current Position
        p = self._get_gcode_position()
        gcmd.respond_raw("X:%.3f Y:%.3f Z:%.3f E:%.3f" % tuple(p))
    def cmd_M220(self, gcmd):
        # Set speed factor override percentage
        value = gcmd.get_float('S', 100., above=0.) / (60. * 100.)
        self.speed = self._get_gcode_speed() * value
        self.speed_factor = value
        import json
        try:
            SAVE = int(gcmd.get('SAVE', 0))
            speed_S = int(gcmd.get_float('S', 100., above=0.))
            v_sd = self.printer.lookup_object('virtual_sdcard')
            speed_mode_path = v_sd.speed_mode_path
            if SAVE==1:
                result = {}
                if speed_S > 100:
                    result["speed_mode"] = 3
                else:
                    result["speed_mode"] = 1
                result["value"] = speed_S
                with open(speed_mode_path, "w") as f:
                    f.write(json.dumps(result))
                    f.flush()
        except Exception as err:
            err_msg = "cmd_M220 err %s" % str(err)
            logging.error(err_msg)
    def cmd_M221(self, gcmd):
        # Set extrude factor override percentage
        new_extrude_factor = gcmd.get_float('S', 100., above=0.) / 100.
        last_e_pos = self.last_position[3]
        e_value = (last_e_pos - self.base_position[3]) / self.extrude_factor
        self.base_position[3] = last_e_pos - e_value * new_extrude_factor
        self.extrude_factor = new_extrude_factor
        import json
        try:
            SAVE = int(gcmd.get('SAVE', 0))
            speed_S = int(gcmd.get_float('S', 100., above=0.))
            v_sd = self.printer.lookup_object('virtual_sdcard')
            if SAVE==1:
                result = {}
                result["value"] = speed_S
                with open(v_sd.flow_rate_path, "w") as f:
                    f.write(json.dumps(result))
                    f.flush()
        except Exception as err:
            err_msg = "cmd_M221 err %s" % str(err)
            logging.error(err_msg)
    cmd_SET_GCODE_OFFSET_help = "Set a virtual offset to g-code positions"
    def cmd_SET_GCODE_OFFSET(self, gcmd):
        move_delta = [0., 0., 0., 0.]
        for pos, axis in enumerate('XYZE'):
            offset = gcmd.get_float(axis, None)
            if offset is None:
                offset = gcmd.get_float(axis + '_ADJUST', None)
                if offset is None:
                    continue
                offset += self.homing_position[pos]
            delta = offset - self.homing_position[pos]
            move_delta[pos] = delta
            self.base_position[pos] += delta
            self.homing_position[pos] = offset
        # Move the toolhead the given offset if requested
        if gcmd.get_int('MOVE', 0):
            speed = gcmd.get_float('MOVE_SPEED', self.speed, above=0.)
            for pos, delta in enumerate(move_delta):
                self.last_position[pos] += delta
            self.move_with_transform(self.last_position, speed)
    def recordPrintFileName(self, path, file_name, fan_state={}, filament_used=0, last_print_duration=0, pressure_advance=""):
        import json, os
        fan = {}
        M204_accel = ""
        old_filament_used = 0
        old_last_print_duration = 0
        old_pressure_advance = ""
        set_gcode_offset = -5
        if os.path.exists(path):
            with open(path, "r") as f:
                result = (json.loads(f.read()))
                # fan = result.get("fan_state", "")
                fan = result.get("fan_state", {})
                M204_accel = result.get("M204", "")
                old_filament_used = result.get("filament_used", 0)
                old_last_print_duration = result.get("last_print_duration", 0)
                set_gcode_offset = result.get("SET_GCODE_OFFSET", -5)
                old_pressure_advance = result.get("pressure_advance", "")
        if fan_state.get("M106 S") and fan_state.get("M106 S", "") != fan.get("M106 S", ""):
            fan["M106 S"] = fan_state.get("M106 S")
        elif fan_state.get("M106 P0") and fan_state.get("M106 P0", "") != fan.get("M106 P0", ""):
            fan["M106 P0"] = fan_state.get("M106 P0")
        elif fan_state.get("M106 P1")  and fan_state.get("M106 P1", "") != fan.get("M106 P1", ""):
            fan["M106 P1"] = fan_state.get("M106 P1")
        elif fan_state.get("M106 P2")  and fan_state.get("M106 P2", "") != fan.get("M106 P2", ""):
            fan["M106 P2"] = fan_state.get("M106 P2")

        if filament_used and filament_used != old_filament_used:
            pass
        else:
            filament_used = old_filament_used
        if last_print_duration and last_print_duration != old_last_print_duration:
            pass
        else:
            last_print_duration = old_last_print_duration
        if pressure_advance and pressure_advance != old_pressure_advance:
            pass
        else:
            pressure_advance = old_pressure_advance
        toolhead = self.printer.lookup_object('toolhead')
        data = {
            'file_path': file_name,
            'absolute_coord': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            # 'fan_state': state,
            'fan_state': fan,
            'M204': M204_accel,
            'filament_used': filament_used,
            'last_print_duration': last_print_duration,
            'SET_GCODE_OFFSET': set_gcode_offset,
            'pressure_advance': pressure_advance,
            'max_accel':toolhead.get_max_accel(),
            'requested_accel_to_decel':toolhead.requested_accel_to_decel,
            'square_corner_velocity':toolhead.square_corner_velocity
        }
        with open(path, "w") as f:
            f.write(json.dumps(data))
            f.flush()
    cmd_CX_RESTORE_GCODE_STATE_help = "Restore a previously saved G-Code state"
    def cmd_CX_RESTORE_GCODE_STATE(self, print_info, file_name_path, XYZET):
        toolhead = self.printer.lookup_object('toolhead')
        try:
            max_accel = toolhead.get_max_accel()
            requested_accel_to_decel = toolhead.requested_accel_to_decel
            square_corner_velocity = toolhead.square_corner_velocity
            state = {
                "absolute_extrude": True,
                "file_position": 0,
                "extrude_factor": 1.0,
                "speed_factor": 0.016,
                "homing_position": [0.0, 0.0, 0.0, 0.0],
                "last_position": [0.0, 0.0, 0.0, 0.0],
                "speed": 25.0,
                "file_path": "",
                "base_position": [0.0, 0.0, 0.0, -0.0],
                "absolute_coord": True,
                # "fan_state": "",
                "fan_state": {},
                "variable_z_safe_pause": 0,
                "M204": "",
                "filament_used": 0,
                "last_print_duration": 0,
                "pressure_advance": "",
                'max_accel':max_accel,
                'requested_accel_to_decel':requested_accel_to_decel,
                'square_corner_velocity':square_corner_velocity
            }
            import os, json
            base_position_e = -1
            state["file_position"] = print_info.get("file_position", 0)
            state["base_position"] = [0.0, 0.0, 0.0, print_info.get("base_position_e", -1)]
            base_position_e = print_info.get("base_position_e", -1)
            logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE base_position_e:%s" % base_position_e)
            with open(file_name_path, "r") as f:
                file_info = json.loads(f.read())
                state["file_path"] = file_info.get("file_path", "")
                state["absolute_extrude"] = file_info.get("absolute_extrude", True)
                state["absolute_coord"] = file_info.get("absolute_coord", True)
                state["fan_state"] = file_info.get("fan_state", {})
                state["variable_z_safe_pause"] = file_info.get("variable_z_safe_pause", 0)
                state["M204"] = file_info.get("M204", "")
                state["SET_GCODE_OFFSET"] = file_info.get("SET_GCODE_OFFSET", -5)
                state["pressure_advance"] = file_info.get("pressure_advance", "")
                state["max_accel"] = file_info.get("max_accel", max_accel)
                state["requested_accel_to_decel"] = file_info.get("requested_accel_to_decel", requested_accel_to_decel)
                state["square_corner_velocity"] = file_info.get("square_corner_velocity", square_corner_velocity)
            # XYZET: {"X": 0, "Y": 0, "Z": 0, "E": 0, "T": ""}
            state["last_position"] = [XYZET["X"], XYZET["Y"], XYZET["Z"], XYZET["E"]+base_position_e]
            logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE state:%s" % str(state))

            # Restore state
            self.absolute_coord = state['absolute_coord']
            # self.absolute_extrude = state['absolute_extrude']
            self.base_position = list(state['base_position'])
            self.homing_position = list(state['homing_position'])
            self.speed = state['speed']
            self.speed_factor = state['speed_factor']
            self.extrude_factor = state['extrude_factor']
            # Restore the relative E position
            logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE base_position:%s" % str(self.base_position))
            e_diff = self.last_position[3] - state['last_position'][3] - 0.7 + 10.0 #5.0
            self.base_position[3] += e_diff 
            logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE self.last_position[3]:%s, state['last_position'][3]:%s, e_diff:%s, \
                         base_position[3]:%s" % (self.last_position[3], state['last_position'][3], e_diff, self.base_position[3]))
            # Move the toolhead back if requested
            gcode = self.printer.lookup_object('gcode')
            if state["fan_state"]:
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE fan fan_state:%s" % str(state["fan_state"]))
                for key in state["fan_state"]:
                    logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE fan set fan:%s#" % str(state["fan_state"].get(key, "")))
                    gcode.run_script_from_command(state["fan_state"].get(key, ""))
                # gcode.run_script_from_command(state["fan_state"])
            logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE before G28 X Y self.last_position:%s" % str(self.last_position))
            gcode.run_script_from_command("G28 X Y")
            logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE after G28 X Y self.last_position:%s" % str(self.last_position))
            x = self.last_position[0]
            y = self.last_position[1]
            z = state['last_position'][2] + self.variable_safe_z + state["variable_z_safe_pause"]
            logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE self.last_position[2]:%s, state['last_position'][2]:%s, self.variable_safe_z:%s, \
                state['variable_z_safe_pause']:%s" % (self.last_position[2], state['last_position'][2], self.variable_safe_z, state["variable_z_safe_pause"]))
            toolhead = self.printer.lookup_object("toolhead")
            diff_z_offset = 0
            if self.config.has_section("z_align"):
                abs_flag = 1
                if os.path.exists(self.absolute_extrude_flag_path):#判断是E轴绝对坐标还是相对坐标打印
                    try:
                        with open(self.absolute_extrude_flag_path, "r") as f:
                            abs_flag = int(json.loads(f.read()).get("absolute_extrude_flag"))
                    except Exception as err:
                        logging.error(err)
                        os.remove(self.absolute_extrude_flag_path)
                if abs_flag == 0:
                    gcode.run_script_from_command("M83")
                else:
                    gcode.run_script_from_command("M82")
                gcode.respond_info("ads_flag:%d"%(abs_flag))
                gcode.run_script_from_command("BED_MESH_CLEAR")
                self.heater_hot = self.printer.lookup_object('extruder').heater
                target_hot_temp_old = self.heater_hot.target_temp
                z_align = self.printer.lookup_object('z_align')
                #diff_z_offset = self.config.getsection('z_align').getfloat('diff_z_offset')
                #根据z_tilt的值调整 diff_z_offset
                try:
                    z_tilt = self.printer.lookup_object('z_tilt')
                    adjustments = z_tilt.get_adjustments()
                    logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE z_tilt.get_adjustments:%s" % str(adjustments))
                    if adjustments:
                        negative = False
                        if abs(adjustments[0]) < abs(adjustments[1]):
                            if adjustments[1] < 0:
                                negative = True
                        else:
                            if adjustments[0] < 0:
                                negative = True
                        adjustments_diff = abs(adjustments[0]-adjustments[1])/2
                        adjustments_diff = adjustments_diff*(-1.0) if negative else adjustments_diff
                except Exception as err:
                    logging.exception("RESTORE z_tilt.get_adjustments err:%s" % err)
                if adjustments_diff != 0 and abs(adjustments_diff)>4.0:
                    logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE adjustments_diff:%s > 3.0" % adjustments_diff)
                    adjustments_diff =  adjustments_diff/10
                diff_z_offset = adjustments_diff
                #end
                za = self.read_real_zmax() + diff_z_offset
                gcode = self.printer.lookup_object('gcode')
                gcmd = gcode.create_gcode_command("", "", {})
                z_align.cmd_ZDOWN(gcmd)
                # 找平
                phoming = self.printer.lookup_object('homing')
                gcode.run_script_from_command("SET_KINEMATIC_POSITION Z=100")
                gcode.run_script_from_command("M400")
                #phoming.resume_adjustment()
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE BED_MESH_PROFILE LOAD='default'")
                gcode.run_script_from_command('BED_MESH_PROFILE LOAD="default"')
                toolhead.set_position([x, y, za, self.last_position[3]], homing_axes=(2,))
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE toolhead.set_position:%s" % str([x, y, za, self.last_position[3]]))
                # gcode.run_script_from_command("M220 S100")
                # gcode.run_script_from_command("G1 X130 Y130 Z%s F600" % (z))
                # gcode.run_script_from_command("M400")
                # gcode.run_script_from_command("M220 S20")
                # gcode.run_script_from_command("M400")
                gcode.run_script_from_command("M109 S%d" %target_hot_temp_old)
                speed = self.speed
                self.last_position[:3] = state['last_position'][:3]
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE G1 X%s Y%s F3000" % (state['last_position'][0], state['last_position'][1]))
                gcode.run_script_from_command("G1 X%s Y%s F3000" % (state['last_position'][0], state['last_position'][1]))
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE move_with_transform:%s, speed:%s" % (self.last_position, speed))
                self.move_with_transform(self.last_position, speed)
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE G1 X%s Y%s F3000" % (state['last_position'][0], state['last_position'][1]))
                gcode.run_script_from_command("G1 X%s Y%s F3000" % (state['last_position'][0], state['last_position'][1]))
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE M400")

            else:
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE BED_MESH_PROFILE LOAD='default'")
                gcode.run_script_from_command('BED_MESH_PROFILE LOAD="default"')
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE toolhead.set_position:%s" % str([x, y, z, self.last_position[3]]))
                toolhead.set_position([x, y, z, self.last_position[3]], homing_axes=(2,))
                speed = self.speed
                self.last_position[:3] = state['last_position'][:3]
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE G1 X%s Y%s Z%s F3000" % (state['last_position'][0], state['last_position'][1], state['last_position'][2]))
                gcode.run_script_from_command("G1 X%s Y%s Z%s F3000" % (state['last_position'][0], state['last_position'][1], state['last_position'][2] - 0.1))
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE move_with_transform:%s, speed:%s" % (self.last_position, speed))
                self.move_with_transform(self.last_position, speed)
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE G1 X%s Y%s Z%s F3000" % (state['last_position'][0], state['last_position'][1], state['last_position'][2]))
                gcode.run_script_from_command("G1 X%s Y%s Z%s F3000" % (state['last_position'][0], state['last_position'][1], state['last_position'][2] - 0.1))
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE M400")
            gcode.run_script_from_command("M400")
            if state["M204"]:
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE SET M204:%s#" % state["M204"])
                gcode.run_script_from_command(state["M204"])
            self.absolute_extrude = state['absolute_extrude']
            try:
                if os.path.exists(gcode.exclude_object_info):
                    reactor = self.printer.get_reactor()
                    with open(gcode.exclude_object_info, "r") as f:
                        exclude_object_cmds = json.loads(f.read())
                        EXCLUDE_OBJECT_DEFINE = exclude_object_cmds.get("EXCLUDE_OBJECT_DEFINE", [])
                        EXCLUDE_OBJECT = exclude_object_cmds.get("EXCLUDE_OBJECT", [])
                        for line in EXCLUDE_OBJECT_DEFINE:
                            reactor.pause(reactor.monotonic() + 0.001)
                            gcode.run_script_from_command(line)
                        for line in EXCLUDE_OBJECT:
                            reactor.pause(reactor.monotonic() + 0.001)
                            gcode.run_script_from_command(line)
                        gcode.run_script_from_command("M400")
            except Exception as err:
                logging.exception("RESTORE EXCLUDE_OBJECT err:%s" % err)
            try:
                if state["SET_GCODE_OFFSET"] != -5:
                    if state["SET_GCODE_OFFSET"] > 0:
                        params = "-%.3f" % state["SET_GCODE_OFFSET"]
                    elif state["SET_GCODE_OFFSET"] < 0:
                        params = "%.3f" % abs(state["SET_GCODE_OFFSET"])
                    else:
                        params = "0"
                    gcode.run_script_from_command("SET_GCODE_OFFSET Z_ADJUST=%s MOVE=0" % params)
                    gcode.run_script_from_command("Z_OFFSET_APPLY_PROBE")
                    gcode.run_script_from_command("M400")
                    logging.info("power_loss SET_GCODE_OFFSET Z_ADJUST:-%s MOVE=0" % state["SET_GCODE_OFFSET"])
            except Exception as err:
                logging.error("RESTORE SET_GCODE_OFFSET err:%s" % err)
            if state["pressure_advance"]:
                gcode.run_script_from_command("M400")
                logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE SET pressure_advance:%s#" % state["pressure_advance"])
                gcode.run_script_from_command(state["pressure_advance"])
            toolhead.set_max_accel(state["max_accel"])
            toolhead.requested_accel_to_decel = state["requested_accel_to_decel"]
            toolhead.square_corner_velocity = state["square_corner_velocity"]
            logging.info("power_loss max_accel=%s requested_accel_to_decel=%s square_corner_velocity=%s" % (
                toolhead.get_max_accel(),
                toolhead.requested_accel_to_decel,
                toolhead.square_corner_velocity
            ))
            gcode.run_script_from_command("BOX_POWER_LOSS_RESTORE")
            if XYZET["T"]:
                gcode.run_script_from_command("M400")
                # gcode.run_script_from_command("BOX_POWER_LOSS_RESTORE")
                gcode.run_script_from_command(XYZET["T"])
                gcode.run_script_from_command("M400")
            logging.info("power_loss cmd_CX_RESTORE_GCODE_STATE done")
        except Exception as err:
            logging.exception("cmd_CX_RESTORE_GCODE_STATE err:%s" % err)
    def read_real_zmax(self):
        import os,json
        data = 300 #350
        if self.config.has_section("z_tilt"):
            z_tilt = self.printer.lookup_object('z_tilt')
            if os.path.exists(z_tilt.real_zmax_path):
                try:
                    with open(z_tilt.real_zmax_path, "r") as f:
                        data = json.loads(f.read()).get("zmax", 0)
                except Exception as err:
                    logging.error(err)
        return data   
    cmd_SAVE_GCODE_STATE_help = "Save G-Code coordinate state"
    def cmd_SAVE_GCODE_STATE(self, gcmd):
        state_name = gcmd.get('NAME', 'default')
        self.saved_states[state_name] = {
            'absolute_coord': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'base_position': list(self.base_position),
            'last_position': list(self.last_position),
            'homing_position': list(self.homing_position),
            'speed': self.speed, 'speed_factor': self.speed_factor,
            'extrude_factor': self.extrude_factor,
        }
    cmd_RESTORE_GCODE_STATE_help = "Restore a previously saved G-Code state"
    def cmd_RESTORE_GCODE_STATE(self, gcmd):
        state_name = gcmd.get('NAME', 'default')
        state = self.saved_states.get(state_name)
        if state is None:
            raise gcmd.error("""{"code":"key274", "msg": "Unknown g-code state: %s", "values":["%s"]}""" % (state_name, state_name))
        # Restore state
        self.absolute_coord = state['absolute_coord']
        self.absolute_extrude = state['absolute_extrude']
        self.base_position = list(state['base_position'])
        self.homing_position = list(state['homing_position'])
        self.speed = state['speed']
        self.speed_factor = state['speed_factor']
        self.extrude_factor = state['extrude_factor']
        # Restore the relative E position
        e_diff = self.last_position[3] - state['last_position'][3]
        self.base_position[3] += e_diff
        # Move the toolhead back if requested
        if gcmd.get_int('MOVE', 0):
            speed = gcmd.get_float('MOVE_SPEED', self.speed, above=0.)
            self.last_position[:3] = state['last_position'][:3]
            self.move_with_transform(self.last_position, speed)
    cmd_GET_POSITION_help = (
        "Return information on the current location of the toolhead")
    def cmd_GET_POSITION(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            raise gcmd.error("""{"code": "key283", "msg": ""Printer not ready"}""")
        kin = toolhead.get_kinematics()
        steppers = kin.get_steppers()
        mcu_pos = " ".join(["%s:%d" % (s.get_name(), s.get_mcu_position())
                            for s in steppers])
        cinfo = [(s.get_name(), s.get_commanded_position()) for s in steppers]
        stepper_pos = " ".join(["%s:%.6f" % (a, v) for a, v in cinfo])
        kinfo = zip("XYZ", kin.calc_position(dict(cinfo)))
        kin_pos = " ".join(["%s:%.6f" % (a, v) for a, v in kinfo])
        toolhead_pos = " ".join(["%s:%.6f" % (a, v) for a, v in zip(
            "XYZE", toolhead.get_position())])
        gcode_pos = " ".join(["%s:%.6f"  % (a, v)
                              for a, v in zip("XYZE", self.last_position)])
        base_pos = " ".join(["%s:%.6f"  % (a, v)
                             for a, v in zip("XYZE", self.base_position)])
        homing_pos = " ".join(["%s:%.6f"  % (a, v)
                               for a, v in zip("XYZ", self.homing_position)])
        gcmd.respond_info("mcu: %s\n"
                          "stepper: %s\n"
                          "kinematic: %s\n"
                          "toolhead: %s\n"
                          "gcode: %s\n"
                          "gcode base: %s\n"
                          "gcode homing: %s"
                          % (mcu_pos, stepper_pos, kin_pos, toolhead_pos,
                             gcode_pos, base_pos, homing_pos))

    cmd_SET_POSITION_help = (
        "SET_POSITION information on the current location of the toolhead")
    def cmd_SET_POSITION(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            raise gcmd.error("""{"code": "key283", "msg": ""Printer not ready"}""")
        position = toolhead.get_position()
        x = position[0]
        y = position[1]
        z = position[2]
        e = position[3]
        X = gcmd.get_float('X', x)
        Y = gcmd.get_float('Y', y)
        Z = gcmd.get_float('Z', z)
        E = gcmd.get_float('E', e)
        toolhead.set_position([X, Y, Z, E], homing_axes=(2,))
        position = toolhead.get_position()
        msg = "toolhead get_position X:%s, Y:%s, Z:%s, E:%s" % (position[0], position[1], position[2], position[3])
        gcmd.respond_info(msg)
def load_config(config):
    return GCodeMove(config)
