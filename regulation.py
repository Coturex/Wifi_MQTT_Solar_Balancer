#!/usr/bin/python3
#!/usr/bin/env python

# Copyright (C) 2018-2019 Pierre Hebert
#                 Mods -> Coturex - F5RQG

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# WARNING: this software is exactly the one in use in the photovoltaic optimization project. It's tailored for my
#          own use and requires minor adaptations and configuration to run in other contexts.

# This is the main power regulation loop. It's purpose is to match the power consumption with the photovoltaic power.
# Using power measurements and a list of electrical equipments, with varying behaviors and control modes, the regulation
# loop takes decisions such as allocating more power to a given equipment when there is photovoltaic power in excess, or
# shutting down loads when the overall power consumption is higher than the current PV power supply.

# Beside the regulation loop, this software also handles these features
# - manual control ("force"), in order to be able to 'manually' turn on/off a given equipment with a specified power and
#   duration. (can be bind with Domotics device topic)
# - monitoring: sends a JSON status message on a MQTT topic for reporting on the current regulation state
# - fallback: a very specific feature which aim is to make sure that the water heater receives enough water (either
#   from the PV panels or the grid to keep the water warm enough.

# See the "equipment module" for the definitions of the loads.


import datetime
import json
import time

import paho.mqtt.client as mqtt

from debug_log import log as log
from debug_log import debug as debug

import cloud_prediction
from cloud_prediction import TOMORROW, Prediction

import equipment
from equipment import ConstantPowerEquipment, UnknownPowerEquipment, VariablePowerEquipment

import configparser
config = configparser.ConfigParser()
config.read('config.ini')

# A debug switch to toggle simulation (uses distinct MQTT topics for instance)
if (config['debug']['simulation'].lower() == "true"):
        SIMULATION = True
        print("**** SIMULATION IS SET")
else:
        SIMULATION = False

if (config['debug']['regulation_debug'].lower() == "true"): 
    SDEBUG = True 
else: SDEBUG = False

last_evaluation_date = None
last_injection = -1
power_production = None
power_consumption = None

equipments = None
equipment_water_heater = None

weather = Prediction(config['openweathermap']['location'],config['openweathermap']['key'])

###############################################################
# MQTT      
mqtt_client = None
prefix = 'simu/' if SIMULATION else ''
MQTT_BROKER = config['mqtt']['broker_ip'] 
PORT = int(config['mqtt']['port'])
TOPIC_SENSOR_CONSUMPTION =  config['mqtt']['topic_cons'] 
TOPIC_SENSOR_PRODUCTION = config['mqtt']['topic_prod'] 
TOPIC_REGULATION = prefix + config['mqtt']['topic_regul'] 
TOPIC_REGULATION_MODE = "NOT_YET_IMPLEMENTED" # forced/unforced duration - Can be bind to domotics device topic 
TOPIC_STATUS = config['mqtt']['topic_regul'] + "/status"
 
###############################################################
# DOMOTICZ
TOPIC_DOMOTICZ_IN = "domoticz/in"
IDX_INJECTION = config['domoticz']['idx_injection']

###############################################################
# EVELUATION
# The comparison between power consumption and production is done every N seconds, it must be above the measurement
# rate, which is currently 4s with the PZEM-004t module.
EVALUATION_PERIOD = int(config['evaluate']['period'])
# Consider powers are balanced when the difference is below this value (watts). This helps prevent fluctuations.
BALANCE_THRESHOLD = int(config['evaluate']['balance_threshold'])
# Keep this margin (in watts) between the power production and consumption. This helps in reducing grid consumption
# knowing that there may be measurement inaccuracy.
MARGIN = int(config['evaluate']['margin'])
LOW_ECS_ENERGY_TWO_DAYS = int(config['evaluate']['low_ecs_energy_two_days'])  # minimal power on two days
LOW_ECS_ENERGY_TODAY = int(config['evaluate']['low_ecs_energy_today']) # minimal power for today

###############################################################
# FUNCTIONS

def now_ts():
    return time.time()

def get_equipment_by_name(name):
    for e in equipments:
        if e.name == name:
            return e
    return None

def on_connect(client, userdata, flags, rc):
    debug(0, "Connected to BROKER " + MQTT_BROKER )
    debug(1, "Subscribing " + TOPIC_SENSOR_CONSUMPTION)
    debug(1, "Subscribing " + TOPIC_SENSOR_PRODUCTION)
    #debug(1, "Subscribing " + TOPIC_REGULATION_MODE)
    client.subscribe(TOPIC_SENSOR_CONSUMPTION)
    client.subscribe(TOPIC_SENSOR_PRODUCTION)
    #client.subscribe(TOPIC_REGULATION_MODE)

def on_message(client, userdata, msg):
    # Receive power consumption and production values and triggers the evaluation. We also take into account manual
    # control messages in case we want to turn on/off a given equipment.
    global power_production, power_consumption
    print("[on message] topic : " + msg.topic) if SDEBUG else ''
    try:
        if msg.topic == TOPIC_SENSOR_CONSUMPTION:
            j = json.loads(msg.payload.decode())
            power_consumption = int(j['power'])
            evaluate()
        elif msg.topic == TOPIC_SENSOR_PRODUCTION:
            j = json.loads(msg.payload.decode())
            power_production = int(j['power'])
            evaluate()
        elif msg.topic == TOPIC_REGULATION_MODE: #NOT YET IMPLEMENTED
            j = json.loads(msg.payload.decode())
            command = j['command']
            name = j['name']
            if command == 'force':
                e = get_equipment_by_name(name)
                if e:
                    power = j['power']
                    msg = 'forcing equipment {} to {}W'.format(name, power)
                    duration = j.get('duration')  # duration is optional with default value None
                    if duration:
                        msg += ' for '+str(duration)+' seconds'
                    else:
                        msg += ' without time limitation'
                    debug(0, '')
                    debug(0, msg)
                    e.force(power, duration)
                    evaluate()
            elif command == 'unforce':
                e = get_equipment_by_name(name)
                if e:
                    debug(0, '')
                    debug(0, 'not forcing equipment {} anymore'.format(name))
                    e.force(None)
                    evaluate()
        print("[on message] conso : " + str(power_consumption) + ", prod : " + str(power_production)) if SDEBUG else ''
    except:
        print("[on message] error, message not formated (PZEM ERROR...)") if SDEBUG else ''
    
    # print(j)

ECS_energy_yesterday = 0
ECS_energy_today = 0
CLOUD_forecast = 999  # undefined

def low_energy_fallback():
    """ Fallback, when the amount of energy today went below a minimum"""

    # This is a custom and very specific fallback method which aim is to turn on the water heater should the daily
    # solar energy income be below a minimum threshold. We want the water to stay warm.
    # The check is done everyday

    global ECS_energy_yesterday, ECS_energy_today, CLOUD_forecast, power_production

    max_power = equipment_water_heater.max_power
    if (ECS_energy_yesterday + ECS_energy_today) < LOW_ECS_ENERGY_TWO_DAYS and ECS_energy_today < LOW_ECS_ENERGY_TODAY:
        duration = 3600 * (LOW_ECS_ENERGY_TODAY - ECS_energy_today) / max_power
        debug(0, '')
        debug(0, '[low_energy_fallback] ECS Energy Yesterday / Today / Sum : {} / {} / {}'.format(ECS_energy_yesterday, ECS_energy_today, ECS_energy_yesterday + ECS_energy_today))
        debug(1, 'daily energy fallback: forcing equipment {} to {}W for {} seconds'.format(equipment_water_heater.name, max_power, duration))
        equipment_water_heater.force(max_power, duration)
            
        # save the energy so that it can be used in the fallback check tomorrow
        ECS_energy_yesterday = ECS_energy_today
        
def evaluate():
    # This is where all the magic happen. This function takes decision according to the current power measurements.
    # It examines the list of equipments by priority order, their current state and computes which one should be
    # turned on/off.

    global last_evaluation_date, ECS_energy_today, last_injection, CLOUD_forecast

    try:
        t = now_ts()
        if last_evaluation_date is not None:
            # reset energy counters every day
            d1 = datetime.datetime.fromtimestamp(last_evaluation_date)
            d2 = datetime.datetime.fromtimestamp(t)
            if d1.day != d2.day:
                ECS_energy_today = equipment_water_heater.get_energy()
                log(0,"")
                log(0,"[evaluate] Clouds / Production : " + CLOUD_forecast + "% / " + ECS_energy_today)
                CLOUD_forecast = weather.getCloudAvg(TOMORROW)
                log(0,"[evaluate] Clouds Forecast : ", CLOUD_forecast)

                for e in equipments:
                    e.reset_energy()
                    
                # ensure that water stays warm enough
                low_energy_fallback()

            # ensure there's a minimum duration between two evaluations
            if t - last_evaluation_date < EVALUATION_PERIOD:
                return

        last_evaluation_date = t

        if power_production is None or power_consumption is None:
            return

        debug(0, '')
        debug(0, '[evaluate] evaluating power consumption={}, production={}'.format(power_consumption, power_production))

        # Here starts the real work, compare powers
        if power_consumption > (power_production - MARGIN):
            # TOO CONSUMPTION, POWER IS NEEDED, decrease the load
            excess_power = power_consumption - (power_production - MARGIN)
            debug(0, "[evaluate] decreasing global power consumption by {}W".format(excess_power))
            for e in reversed(equipments):
                debug(2, "1. examining " + e.name)
                if e.is_forced():
                    debug(4, "skipping this equipment because it's in forced state")
                    continue
                result = e.decrease_power_by(excess_power)
                if result is None:
                    debug(2, "stopping here and waiting for the next measurement to see the effect")
                    break
                excess_power -= result
                if excess_power <= 0:
                    debug(2, "[no more excess power consumption, stopping here")
                    break
                else:
                    debug(2, "There is {}W left to cancel, continuing".format(excess_power))
            debug(2, "No more equipment to check")
        elif (power_production - MARGIN - power_consumption) < BALANCE_THRESHOLD:
            # Nice, this is the goal: CONSUMPTION is EQUAL to PRODUCTION
            debug(0, "[evaluate] power consumption and production are balanced")
        else:
            # There's PV POWER IN EXCESS, try to increase the load to consume this available power
            available_power = power_production - MARGIN - power_consumption
            debug(0, "[evaluate] increasing global power consumption by {}W".format(available_power))
            for i, e in enumerate(equipments):
                if available_power <= 0:
                    debug(2, "no more available power")
                    break
                debug(2, "2. examining " + e.name)
                if e.is_forced():
                    debug(4, "skipping this equipment because it's in forced state")
                    continue
                result = e.increase_power_by(available_power)
                if result is None:
                    debug(2, "stopping here and waiting for the next measurement to see the effect")
                    break
                elif result == 0:
                    debug(2, "no more available power to use, stopping here")
                    break
                elif result < 0:
                    debug(2, "not enough available power to turn on this equipment, trying to recover power on lower priority equipments")
                    freeable_power = 0
                    needed_power = -result
                    for j in range(i + 1, len(equipments)):
                        o = equipments[j]
                        if o.is_forced():
                            continue
                        p = o.get_current_power()
                        if p is not None:
                            freeable_power += p
                    debug(2, "power used by other equipments: {}W, needed: {}W".format(freeable_power, needed_power))
                    if freeable_power >= needed_power:
                        debug(2, "recovering power")
                        freed_power = 0
                        for j in reversed(range(i + 1, len(equipments))):
                            o = equipments[j]
                            if o.is_forced():
                                continue
                            result = o.decrease_power_by(needed_power)
                            freed_power += result
                            needed_power -= result
                            if needed_power <= 0:
                                debug(2, "enough power has been recovered, stopping here")
                                break
                        new_available_power = available_power + freed_power
                        debug(2, "now trying again to increase power of {} with {}W".format(e.name, new_available_power))
                        available_power = e.increase_power_by(new_available_power)
                    else:
                        debug(2, "this is not possible to recover enough power on lower priority equipments")
                else:
                    available_power = result
                    debug(2, "there is {}W left to use, continuing".format(available_power))
            debug(2, "no more equipment to check")
        
        ##########  
        # Build a Domoticz Injection  message 
        injection = (power_consumption - power_production) 
        print("[evaluate]                    CALCULATED INJECTION :", injection) if SDEBUG else ''
        if injection < 0:
            domoticz = "{ \"idx\": " + IDX_INJECTION + ", \"nvalue\": 0, \"svalue\": \"" + str(injection) + "\"}"
            print (domoticz) if SDEBUG else ''
            mqtt_client.publish(TOPIC_DOMOTICZ_IN, domoticz)
        else: # Send 0 injection only if last_injection wasn't zero in order to avoid repetition
            injection = 0
            if last_injection != 0:
                domoticz = "{ \"idx\": " + IDX_INJECTION + ", \"nvalue\": 0, \"svalue\": \"" + str(injection) + "\"}"
                print (domoticz) if SDEBUG else ''
                mqtt_client.publish(TOPIC_DOMOTICZ_IN, domoticz)
        last_injection = injection
        ##########
        # Build a status message
        status = {
            'date': t,
            'date_str': datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S'),
            'power_consumption': power_consumption,
            'power_production': power_production,
            'injection' : injection
        }
        es = []
        for e in equipments:
            p = e.get_current_power()
            es.append({
                'name': e.name,
                'current_power': 'unknown' if p is None else p,
                'energy': e.get_energy(),
                'forced': e.is_forced()
            })
        status['equipments'] = es
        mqtt_client.publish(TOPIC_STATUS, json.dumps(status))

    except Exception as e:
        debug(0,"[evaluate exception]") 
        debug(1, e)

###############################################################
# MAIN

def main():
    global mqtt_client, equipments, equipment_water_heater
 
    debug(0,"")
    log(0,"")
    log(0,"[Main] Starting PV Power Regulation @" + config['openweathermap']['location'])

    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect(MQTT_BROKER, PORT , 120)

    equipment.setup(mqtt_client, not SIMULATION)
    equipment_water_heater = VariablePowerEquipment('ECS', TOPIC_REGULATION + "/vload/ECS")
    
    # This is a list of EQUIPMENTS BY PRIORITY OREDER (first one has the higher priority). 
    # As many equipments as needed can be listed here.
    equipments = (
        equipment_water_heater,
        #ConstantPowerEquipment('e_bike_charger', 120, "regul/cload/bike" ),
        # UnknownPowerEquipment('plug_1', "regul/uload/topic")
    )

    log(0, "Equipments :")
    # At startup, reset everything - Mandatory !
    for e in equipments:
        e.set_current_power(0) 
        log(1, str(e.name) + " power topic : " + e.topic_set_power)
        log(1, str(e.name) + " power max : " + str(e.max_power) + " W" )

    mqtt_client.loop_forever()

if __name__ == '__main__':
    main()
