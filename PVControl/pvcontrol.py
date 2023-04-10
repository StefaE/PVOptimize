'''
Copyright (C) 2022    Stefan Eichenberger   se_misc ... hotmail.com

This file is part of the PVOptimize project: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
'''

from datetime import datetime, time, timedelta
import pytz
import pandas as pd
import math
from influxdb import DataFrameClient

from .pvmonitorfactory      import PVMonitorFactory
from .wallboxfactory        import WallBoxFactory
from .pvmodel.pvmodel       import PVModel
from .pvforecast.pvforecast import PVForecast

import pickle

class PVControl():
    """
    This class implements a controller for a photovoltaic system. It controls PV excess charing of
    an electrical vehicle and smart home battery charging during PV peak production periods.

    Methods
    -------
    __init__(config)
        Instantiates an object and configures it according to a configparser object 'config'

    runControl(_pvstatus = None, _pvforecast = None, _carstatus = None)
        runs the controller. It can be run in two contexts:
        a/ interactively, through a call of a PV simulator PVServer. In that case, arguments need be
        provided.
        b/ active mode, where hardware is read and controlled. This is typically done through a script
        called from crontab every minute. In this mode, arguments are left to default and runControl()
        collects the required data directly.
    """
    def __init__(self, config):
        self.config           = config
        self.Name             = "PV Controller"
        
        self.phases           = self.config['PVControl'].getint('phases', 3)
        self.I_min            = self.config['PVControl'].getfloat('I_min', None)         # minimum charge current, will be read from wallbox
        self.I_max            = self.config['PVControl'].getfloat('I_max', None)         # maximum charge current supported by wallbox, will be read from wallbox
        self.I_gridMax        = self.config['PVControl'].getfloat('I_gridMax', 0)        # max current we are allowed to get from grid
        self.feedInLimit      = self.config['PVControl'].getint('feedInLimit', 99999)    # power limit (70% rule)
        self.minSOC           = self.config['PVControl'].getfloat('minSOC', 0.05)        # minimum SOC we want to tolerate
        self.batMinSOC        = self.config['PVStorage'].getfloat('minSOC', 0.05)        # minimum SOC supported by battery
        if self.minSOC < self.batMinSOC: self.minSOC = self.batMinSOC
        self.maxSOC           = self.config['PVControl'].getfloat('maxSOC', 1)           # maximum SOC we want to tolerate
        self.minSOCCharge     = self.config['PVControl'].getfloat('minSOCCharge', self.minSOC)   # minimum SOC before PV charge starts
        self.maxSOCCharge     = self.config['PVControl'].getfloat('maxSOCCharge', self.maxSOC)   # maximum SOC during PV charing

        self.chargeNow        = self.config['PVControl'].getboolean('chargeNow', True)   # start charging 'now' if possible
        self.chargeStart      = self.config['PVControl'].getint('chargeStart', 0)        # Epoch (UTC) after which to start charging if possible (nowSwitch = False)
        self.allow_Bat2EV     = self.config['PVControl'].getboolean('allow_Bat2EV', False)

        self.InverterEff      = 0.97
        self.batCapacity      = self.config['PVStorage'].getint('batCapacity')           # battery capacity [Wh]
        self.maxBatDischarge  = self.config['PVStorage'].getint('maxBatDischarge')       # maximum battery (dis-)charge power [W]
        self.maxBatCharge     = self.config['PVStorage'].getint('maxBatCharge', self.maxBatDischarge/self.InverterEff)

        self.coeff_A          = [0.5,  1.0]                                              # coefficients for battery power allowance model
        self.coeff_B          = [0.2,  0.7]
        self.coeff_C          = [1.2,  2.0]                                              # coefficients for battery charge model

        self.pvstatus         = None                                                     # current PV status
        self.pvforecast       = None
        self.wallbox          = None                                                     # hardware abstraction
        self.inverter         = None

        self.monitorProvider  = self.config['PVControl'].get('pvmonitor', 'Kostal')      # which class provides PVMonitor?
        self.wallboxProvider  = self.config['PVControl'].get('wallbox', 'HardyBarth')    # which class provides wallbox?

        self.I_charge         = None
        self.I_bat            = None
        self.inhibitDischarge = False                                                    # don't allow battery discharge
        self.ctrlstatus       = {}
        self.sysstatus        = {}

        try:
            file             = open('./pvcontrol.pickle', 'rb')
            self.persist     = pickle.load(file)
            if not all(key in self.persist.keys() 
                   for key in ('saved', 'ctrl_power', 'overflow_start', 'overflow_end', 'endcharge', 'charge_completed', 'calcSOC')):
                self._initPersist()
            pass
        except:
            self._initPersist()                                                          # file doesn't exist

    def runControl(self, _pvstatus = None, _pvforecast = None, _carstatus = None):
        """
        Runs the controller for one time stamp. Typically, the instance is distroyed after
        dealing with the returned information.

        Parameters are only provided when called from PVServer() for simulation.
        When called in active mode (ie., to actually control hardware), no parameters are provided and 
        corresponding data is collected inside the method.

        Parameters
        ----------
            _pvstatus   : Pandas DataFrame, optional
                describes status of PV system and is returned from PVMonitorTemplate.getStatus()
            _pvforecast : Pandas DataFrame, optional 
                contains PV performance forecast data for the current day, as provided by PVForecast.getForecast()
            _carstatus  : Dictionary, optional
                contains minimal information about a car connected to a (dummy) Wallbox. Keys required are
                'connected' and 'charge_completed' with boolean values.

        Returns
        -------
            self.sysstatus : Dictionary with keys:
                ctrlstatus : Caller (eg. PVServer)can expect to find at least the following keys:
                    fastcharge : boolean
                        fast-charge home battery, or use smart-charge capability of inverter
                    ctrl_power : float
                        total power provided by the controller to wallbox, [W]
                    max_soc    : float, range 0 .. 1
                        battery SOC should not exceed max_soc
                    bat_forecast : float
                        have/need, where 'have' is expected remaining PV power which can be used for battery charging
                        and 'need' is power needed to charge battery to 'max_soc' [W]
                    inhibitDischarge : boolean
                        inhibit battery discharging
                    -- The following properties are used by PVControl GUI project
                    I_charge : float
                        EV charge current requested from wallbox [A] (redundant to ctrl_power)
                    batMinSoc : float, range 0 .. 1
                        battery minimal SOC (defined by battery) for healthy operation
                    chargeNow : boolean
                        start charging now (if all other condtions regarding power are met)? This potentially changes
                        from false to true if chargeStart time is reached.
                    overflow_start : integer
                        seconds after midnight UTC where it is possible (based on clearsky PV forecast) that feed-in
                        overflow is reached
                    Additional keys are used by self._logInflux()
                pvstatus : status as returned from self.monitorProvider
                wbstatus : status as returned from self.wallboxProvider - used by GUI to display status (wallbox specific)
                GUI_control : configuration information for GUI
                    i_charge_min : integer
                        minimum for current sliders
                    i_charge_max : integer
                        maximum for current sliders
                    minsoc : float, range 0 .. 1
                        lowest value for SOC sliders
        """
        self.pvforecast             = _pvforecast
        wallbox                     = WallBoxFactory()
        if _pvstatus is None:       # --------------------------------------------------- we need get life PV status data
            pvmonitor               = PVMonitorFactory()
            self.inverter           = pvmonitor.getPVMonitor(self.monitorProvider, self.config)
            self.pvstatus           = self.inverter.getStatus()
            self.wallbox            = wallbox.getWallBox(self.wallboxProvider, self.config)
            self.wallbox.readWB(self.persist['charge_completed'])
            if self.wallbox.status is not None:
                ctrl_power          = self._I_to_P(self.wallbox.status['ctrl_current'])
                if self.I_min is None or self.I_min < self.wallbox.status['I_min']: self.I_min = self.wallbox.status['I_min']
                if self.I_max is None or self.I_max > self.wallbox.status['I_max']: self.I_max = self.wallbox.status['I_max']
            else:
                ctrl_power          = self.persist['ctrl_power']                         # fall-back
            self.currTime           = self.pvstatus.name
            if self.pvforecast is None:
                forecastObj         = PVForecast(self.config)
                self.pvforecast     = forecastObj.getForecast(self.pvstatus.name)
            active                  = True                                               # we are in active mode, actually controlling wallbox

        else:                       # ----------------------------------------------------- running in simulation mode
            self.pvstatus           = _pvstatus
            self.wallbox            = wallbox.getWallBox('dummy', self.config)
            self.wallbox.status.update(_carstatus)
            self.currTime           = self.pvstatus.name                                 # time of last PV status
            ctrl_power              = self.persist['ctrl_power'] 
            if self.I_min is None: self.I_min = self.wallbox.status['I_min']             # fall-back values
            if self.I_max is None: self.I_max = self.wallbox.status['I_max']
            active                  = False
        req_ctrl_power_prev         = self.persist['ctrl_power']                         # requested control power in previous step

        self.I_min                  = self.wallbox.round_current(self.I_min)
        self.sysstatus['pvcontrol'] = self._getPVControl()
        if self.persist['saved'].year > 1970:
            delta_t                 = (self.currTime - self.persist['saved']).total_seconds()/60
            if delta_t > 10: self._initPersist()                                         # file is older than 10 minutes, re-inialize
        if self.persist['calcSOC'] == 0:                                                 # after creation of persist file
            self.persist['calcSOC'] = self.pvstatus.soc
        elif self.pvstatus.soc  == self.maxSOC:                                          # re-calibrate at full charge
            self.persist['calcSOC'] = self.maxSOC
        else:                                                                            # calculate new soc based on bat_power
            self.persist['calcSOC'] += (-self.pvstatus.bat_power*delta_t/60)/self.batCapacity
        self.ctrlstatus['calcSOC']  = self.persist['calcSOC']

        self._getClearsky()                                                              # determine clearsky parameters
        if self.chargeStart < datetime.timestamp(self.currTime)*1000: 
            self.chargeNow = True
        self._getI_charge(ctrl_power, req_ctrl_power_prev)                               # calculate WB charge current
        fastcharge                       = self._manageBatCharge(ctrl_power)             # calculate max. charge battery power

        self.ctrlstatus['I_charge']      = self.I_charge
        self.ctrlstatus['I_bat']         = self.I_bat
        self.ctrlstatus['fastcharge']    = fastcharge
        self.persist['ctrl_power']       = self._I_to_P(self.I_charge)                   # prepare persistent data structure - will be written to file in __del__
        self.persist['charge_completed'] = self.wallbox.status['charge_completed']
        self.persist['saved']            = self.currTime
        
        if active:                                                                       # actively controll wallbox
            self._logInflux()
            if self.I_max > 0:                                                           # don't control wallbox if I_max == 0
                self.wallbox.controlWB(self.I_charge)
            if self.inverter is not None:
                self.inverter.setBatCharge(fastcharge, self.inhibitDischarge, self.feedInLimit, self.maxBatCharge, self.maxSOC, self.minSOC)
                del self.inverter
        self.ctrlstatus['ctrl_power']       = self._I_to_P(self.I_charge)
        self.ctrlstatus['max_soc']          = self.maxSOC
        self.ctrlstatus['batMinSoc']        = self.batMinSOC
        self.ctrlstatus['inhibitDischarge'] = self.inhibitDischarge
        self.ctrlstatus['chargeNow']        = 1 if self.chargeNow else 0                 # GUI wants an integer
        t                                   = self.persist['overflow_start']
        self.ctrlstatus['overflow_start']   = (t.hour * 3600 + t.minute * 60) * 1000     # milliseconds since midnight (UTC) when overflow can start

        self.sysstatus['ctrlstatus']        = self.ctrlstatus
        self.sysstatus['pvstatus']          = self.pvstatus.to_dict()
        self.sysstatus['wbstatus']          = self.wallbox.status
        return(self.sysstatus)

    def __del__ (self):
        """
        Distructor - main function is to write self.persist into pickle serialization file
        """
        file                 = open('./pvcontrol.pickle', 'wb')
        pickle.dump(self.persist, file)
        pass

    def _initPersist(self):
        """
        re-creates self.pickle from scratch, in case pickle serialization file was not found or older than 10min.
        """
        print("pvcontrol: file pvcontrol.pickle recreated")
        t            = datetime(1970, 1, 1, 0, 0, tzinfo=pytz.utc)
        self.persist = { 'saved'            : t,                                         # time stamp of persistent data
                         'ctrl_power'       : 0,                                         # power delivered to controller in prior step (for sim, as fall-back)
                         'overflow_start'   : time(0, 0),                                # start of time period for potential 70% power limitiation
                         'overflow_end'     : time(0, 0),                                # end   of time period for potential 70% power limitation
                         'endcharge'        : { 1 : None },                              # when can we no longer reach I = 'key'?
                         'charge_completed' : 0,                                         # wallbox charging completed
                         'calcSOC'          : 0                                          # calculated SOC
                        }

    def _getClearsky(self):
        """
        Uses PVModel() to calculate clear-sky estimate for the PV system and stores interesing timestamps into self.persist
        """
        if self.currTime.date() > self.persist['saved'].date():                          # ... new day (just after midnight UTC - assumes midnight UTC is during local night)
            myPVSystem  = PVModel(self.config)
            times       = pd.date_range(self.currTime.replace(hour=0, minute=0, second=0), self.currTime.replace(hour=23), freq="5min")
            times_df    = pd.DataFrame(times).set_index(0)
            clearsky    = myPVSystem.runModel(times_df, 'clearsky')
            endcharge   = {}  
            for I in range(1, math.ceil(self.I_max), 1):
                P       = self._I_to_P(I)
                power   = clearsky.loc[clearsky['ac_clearsky'] > P]
                if not power.empty:                                                      # at what time do we have last time enough clearsky power to generate current I?
                    endcharge[I] = power.iloc[-1].name.time()
            self.persist['endcharge'] = endcharge
            power       = clearsky.loc[clearsky['ac_clearsky'] > self.feedInLimit]       # potential overflow
            if power.empty:                                                              # we are in winter or transition time
                power   = clearsky.loc[clearsky['ac_clearsky'] > 0.9*self.feedInLimit]   # allow a bit of slack for overradiation days
            if power.empty:                                                              # we are in winter
                self.persist['overflow_start'] = time(23, 59)                                            # no power limit period
                self.persist['overflow_end']   = time( 0,  0)
            else:
                self.persist['overflow_start'] = (power.iloc[0].name  - timedelta(minutes=30)).time()    # give 30min slack for over-radiation
                self.persist['overflow_end']   = (power.iloc[-1].name + timedelta(minutes=30)).time()
            print('power_limit for ' + str(self.currTime.date()) + ': ' + str(self.persist['overflow_start'])[0:5] + " .. " + str(self.persist['overflow_end'])[0:5])
        return()

    def _getI_charge(self, ctrl_power, req_ctrl_power_prev = None):
        """
        Determine current for EV excess charging. This method is the core of the smart PV excess charging algorithm
        
        Parameters
        ----------
            ctrl_power : float
                power currently delivered by wallbox (based on calculations from previous time stamp)
            req_ctrl_power_prev : float
                requested control power in previous step; normally that should be ctrl_power, but if EV
                is unable to consume all requested power, this maybe smaller.
        """
        I_bat                     = 0
        if req_ctrl_power_prev is None:
            req_ctrl_power_prev   = ctrl_power
        if self.chargeNow and self.wallbox.status['connected']:
            I_prev                = self._P_to_I(ctrl_power)                             # what we have been charging so far
            I_prev_req            = self._P_to_I(req_ctrl_power_prev)
            if abs(self.I_min - I_prev) < 0.1:                                           # we suffer from rounding errors
                I_prev            = self.I_min
            if abs(I_prev_req - I_prev) < 0.1:
                I_prev_req        = I_prev
            avail_P               = self.pvstatus.dc_power*self.InverterEff - self.pvstatus.home_consumption + ctrl_power
            if avail_P < 0: avail_P = 0                                                  # negative: no PV power available at all
            I_maxPV               = self._P_to_I(avail_P)
            I_missing             = 0
            if ctrl_power > 0  and I_maxPV < self.I_min:                                 # if we can supply that much power, we are mid-way between previous and min
                I_missing         = (I_prev + self.I_min)/2 - I_maxPV 
            if ctrl_power == 0 and (I_maxPV + self.I_gridMax >= self.I_min or self.allow_Bat2EV):    # try to harvest battery and grid
                I_missing         = self.I_min - I_maxPV                                 # ... at least this much we need find
            if I_missing > 0:
                if self.allow_Bat2EV and self.pvstatus.soc > self.minSOCCharge:          # allow charing EV from battery
                    I_bat    = self._P_to_I(self.maxBatDischarge)
                    I_charge = self.I_gridMax + I_bat + I_maxPV
                else:
                    I_bat             = self._maxFromBat(self.coeff_A)                   # current we can supply, using Coeff_A, to supply >I_min
                    if I_missing > I_bat:                                                # we don't want provide so much from the battery
                        I_missing     = self.I_min - I_maxPV                             # if at least we can get this, we can continue with charging
                        I_bat         = self._maxFromBat(self.coeff_B)                   # max. avail current based on Coeff_B to sustain I_min
                        if I_missing > I_bat + self.I_gridMax:                           # we can't supply from battery alone, or battery plus grid allowence
                            I_missing = 0
                        elif I_missing <= self.I_gridMax:                                # if grid allowence itself is sufficient
                            self.inhibitDischarge = True                                 # don't use battery
                    elif I_missing <= self.I_gridMax:
                        self.inhibitDischarge = True
                    if self.inhibitDischarge and self.I_gridMax - I_maxPV > I_missing:
                        I_missing = self.I_gridMax                                       # ok., let's use all grid power we can (but limit below to self.I_max)
                    I = math.floor(self.I_min - self.I_gridMax)                          # will not be able to charge anymore without battery
                    if I in self.persist['endcharge']:
                        t = self.persist['endcharge'][I]
                        if self.currTime.time() > t:
                            I_missing = 0
                    I_charge          = I_maxPV + I_missing                              # how much we want supply - this may include some grid power
                    if I_prev > 0 and I_charge > I_prev and not self.inhibitDischarge:   # we have something missing (not feeding from grid only), still increase I_charge?
                        I_charge = I_prev                                                # .. this should only be due to rounding errors
            else:  I_charge       = I_maxPV
            I_charge = self.wallbox.round_current(I_charge)                              # HardyBarth rounds down to full amps

            if I_charge < self.I_min: 
                if I_prev < I_prev_req: I_charge = self.I_min                            # we requested more than was consumed ... 
                else:                   I_charge = 0                                     # we are below the limit which WB can deliver for charging
            if I_charge > self.I_max: I_charge = self.I_max                              # we can't charge with more current than this
            self.I_charge          = I_charge
        else:
            self.I_charge          =  0
            avail_P                = -1
        self.ctrlstatus['avail_P'] = avail_P
        self.I_bat                 = I_bat
        return()

    def _manageBatCharge(self, ctrl_power):
        """
        Manage smart battery charging. This method is the core of the smart home battery charging algorithm

        Parameters
        ----------
            ctrl_power : float
                power currently delivered by wallbox (based on calculations from previous time stamp)
        """
        need           = 0
        have           = 0
        fastcharge     = True                                                            # default if no forecast data available

        if self.pvforecast is not None:
            fastcharge = False                                                           # default if forecast data available
            need       = (self.maxSOC - self.pvstatus.soc)*self.batCapacity              # needed energy to charge battery [kWh]
            if need < 0: need = 0                                                        # .. in case maxSOC changed
            try:
                next   = self.pvforecast[self.pvforecast['period_end'] >= self.currTime].iloc[0]   # this gives forecast for the next forecast time stamp
                prev   = self.pvforecast.loc[next.name + 1]                              # this gives forecast for the previous forecast time stamp ('name' was index value in data frame)
                dt     = next['period_end'] - prev['period_end']                         # interval duration
                now    = self.currTime - prev['period_end']
                dP     = next['remain'] - prev['remain']                                 # remaining power loss during interval
                have   = prev['remain'] + dP*now/dt                                      # how much power do we have right now?
                end_pv = self.pvforecast[self.pvforecast['remain'] < 100].iloc[0]['period_end']
                dt_pv  = (end_pv - self.currTime).total_seconds()/3600                   # how long do we still have PV power? [h]
                if dt_pv < 0: dt_pv = 0                                                  # ... for if we are past sunset
                home   = self.pvstatus.home_consumption - ctrl_power                     # current home consumption (without what goes to wallbox), [W]
                have   = have - home*dt_pv                                               # subtract home consumption (at current level) from available PV forecast
                if have < 0: have = 0
            except Exception:
                need   = 0
                have   = 0

            if self.wallbox.status['connected'] and self.wallbox.status['charge_completed']:
                self.inhibitDischarge = False
                self.minSOCCharge     = self.minSOC                                      # don't fastcharge to minSOCCharge anymore; wait for 'need' big enough or 'overflow_end'

            if not (self.allow_Bat2EV and self.wallbox.status['connected'] and self.I_charge > 0 and not self.wallbox.status['charge_completed']):
                                                                                         # if we allow_Bat2EV and are connected, can supply current and are not completed, we don't allow fastcharge
                if self.pvstatus.dc_power > self.feedInLimit/50:                         # we have some PV power ...
                    if need > have/self.coeff_C[0] and self.inhibitDischarge == False:   # oops - we should start focusing on battery now (unless grid charging is on)
                        fastcharge            = True
                        self.I_charge         = self.I_charge - self._P_to_I(self.maxBatCharge)                   # stop charging car
                        if self.I_charge < self.I_min: self.I_charge = 0
                    elif self.minSOCCharge > self.pvstatus.soc:                                                   # enforce fastcharge to minSOCCharge
                        fastcharge            = True
                        self.I_charge         = 0                                                                 # focus on bringing battery to minSOCCharge
                    elif self.wallbox.status['connected'] and not self.wallbox.status['charge_completed']:        # planning to / ongoing car charge - all surplus goes to battery
                        if self.pvstatus.soc < self.maxSOCCharge or self.currTime.time() > self.persist['overflow_end']: 
                            fastcharge = True
                        if need < have/self.coeff_C[1] and self.currTime.time() < self.persist['overflow_start']: # don't charge full yet, whilst charging car
                            self.maxSOC       = self.maxSOCCharge
                    elif need > have/self.coeff_C[1] and self.currTime.time() < self.persist['overflow_end']:     # still early, but not that much more energy left ...
                        fastcharge            = True
                    elif self.currTime.time() > self.persist['overflow_end']:            # afternoon - charge battery now without further condition
                        fastcharge            = True
                else:   fastcharge            = True                                     # ... if we are here, we probably won't load battery anyway, but we may at least try ...
                if self.I_charge == 0: self.I_bat = 0
        self.ctrlstatus['need']       = need
        self.ctrlstatus['have']       = have
        if need > 0: self.ctrlstatus['bat_forecast'] = have/need
        else:        self.ctrlstatus['bat_forecast'] = 1
        return(fastcharge)

    def _P_to_I(self, P):
        """
        Convert AC power to current (per phase)
        """
        I = P / (self.pvstatus.grid_voltage * self.phases)
        return(I)

    def _I_to_P(self, I):
        """
        Convert current (per phase) to AC power
        """
        P = I * self.pvstatus.grid_voltage * self.phases
        return(P)

    def _maxFromBat(self, coeff):
        """
        determines how much battery power we want to use for EV charging. This is a function of
        some coefficients 'coeff' and depends on current battery SOC.
        """
        I_batMax  = self._P_to_I(self.maxBatDischarge)
        a         = I_batMax/(coeff[1]-coeff[0])
        b         = -a*coeff[0]
        if self.pvstatus.soc > self.minSOCCharge:
            I_bat = self.pvstatus.soc*a+b                                                # max. avail current based on coeff. a1, a2 to slowly reduce charing
            if I_bat < 0:        I_bat = 0
            if I_bat > I_batMax: I_bat = I_batMax
        else:
            I_bat = 0    
        return(I_bat)

    def _logInflux(self):
        """
        Log controller information to Influx. Three measurements are created (below).
        It also sets a corresponding JSON structure in self.sysstatus

        Measurements
        ------------
            wbstatus   : Wallbox status
                mandatory field description see Class WBTemplate description
                additional fields maybe present in self.wbstatus as created by the active PVMonitor provider
            
            pvstatus   : PV system status
                mandatory field description see Class PVMonitorTemplate description
                additional fields maybe present in self.pvstatus as created by the active PVMonitor provider
            
            ctrlstatus : Controller status with fields:
                avail_P      : current available PV excess power
                I_charge     : current intended WB charge current (see _getI_charge())
                I_bat        : how much current is allowed to be taken from battery to charge EV
                have         : forecasted excess PV power for rest of day
                need         : needed power to charge home battery to maxSOC
                bat_forecast : have/need
                calcSOC      : calculated home battery SOC based on battery currents and voltages
                fastcharge   : fast-charge (or smart-charge) home battery
        """
        
        host = self.config['PVControl'].get('host', None)
        if host is not None:
            try:
                inhibit  = self.config['PVControl'].getboolean('inhibitInflux', False)   # inhibit writing to Influx DB
                if not inhibit:
                    port     = self.config['PVControl'].getint('port', 8086)
                    database = self.config['PVControl'].get('database')
                    client   = DataFrameClient(host=host, port=port, database=database)

                    df       = pd.DataFrame(self.wallbox.status, index = [self.currTime])
                    df.drop(['I_min', 'I_max'], axis=1, inplace=True)
                    for field in df:
                        df[field] = df[field].astype(float)
                    client.write_points(df, 'wbstatus')

                    df       = pd.DataFrame(self.pvstatus.to_frame().transpose())
                    df.drop(['minSoc'], axis=1, inplace=True)
                    for field in df:
                        df[field] = df[field].astype(float)
                    client.write_points(df, 'pvstatus')

                    df       = pd.DataFrame(self.ctrlstatus, index = [self.currTime])
                    for field in df:
                        df[field] = df[field].astype(float)
                    client.write_points(df, 'ctrlstatus')
                    pass
            except Exception as e:
                print('pvcontrol._logInflux: ' + str(e))

    def _getPVControl(self):
        """
        get PVControl settings for sysstatus (as later used by GUI)
        """

        pvcontrol = { "I_min"        : self.I_min,
                      "I_max"        : self.I_max,
                      "I_gridMax"    : self.I_gridMax,

                      "minSOC"       : self.minSOC,
                      "minSOCCharge" : self.minSOCCharge,
                      "maxSOCCharge" : self.maxSOCCharge,
                      "maxSOC"       : self.maxSOC,
                      "allow_Bat2EV" : 1 if self.allow_Bat2EV else 0                     # we need an integer
                    }
        return pvcontrol 