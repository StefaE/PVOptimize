'''
Copyright (C) 2021    Stefan Eichenberger   se_misc ... hotmail.com

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

from datetime import datetime, timezone, date, time, timedelta
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
        self.config          = config
        self.Name            = "PV Controller"
        
        self.phases          = self.config['PVControl'].getint('phases', 3)
        self.I_min           = self.config['PVControl'].getfloat('I_min', None)          # minimum charge current, will be read from wallbox
        self.I_max           = self.config['PVControl'].getfloat('I_max', None)          # maximum charge current supported by wallbox, will be read from wallbox
        self.I_gridMax       = self.config['PVControl'].getfloat('I_gridMax', 0)         # max current we are allowed to get from grid
        self.feedInLimit     = self.config['PVControl'].getint('feedInLimit', 99999)     # power limit (70% rule)
        self.maxSOC          = self.config['PVControl'].getfloat('maxSOC', 1)
        self.maxSOCCharge    = self.config['PVControl'].getfloat('maxSOCCharge', self.maxSOC)

        self.InverterEff     = 0.97
        self.batCapacity     = self.config['PVStorage'].getint('batCapacity')            # battery capacity [Wh]
        self.maxBatDischarge = self.config['PVStorage'].getint('maxBatDischarge')        # maximum battery (dis-)charge power [W]
        self.maxBatCharge    = self.config['PVStorage'].getint('maxBatCharge', self.maxBatDischarge/self.InverterEff)
        self.minSOC          = self.config['PVStorage'].getfloat('minSOC', 0.05)

        self.coeff_A         = [0.5, 1.0]                                                # coefficients for battery power allowence model
        self.coeff_B         = [0.2, 0.7]
        self.coeff_C         = [1.2,   2]                                                # coefficients for battery charge model

        self.pvstatus        = None                                                      # current PV status
        self.pvforecast      = None
        self.wallbox         = None                                                      # hardware abstraction
        self.inverter        = None

        self.monitorProvider = self.config['PVControl'].get('pvmonitor', 'Kostal')       # which class provides PVMonitor?
        self.wallboxProvider = self.config['PVControl'].get('wallbox', 'HardyBarth')     # which class provides wallbox?

        self.I_charge        = None
        self.ctrlstatus      = {}

        try:
            file             = open('./pvcontrol.pickle', 'rb')
            self.persist     = pickle.load(file)
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
            self.ctrlstatus : Dictionary
                Caller can expect to find at least the following keys:
                fastcharge : boolean
                    fast-charge home battery, or use smart-charge capability of inverter
                ctrl_power : float
                    total power provided by the controller to wallbox, [W]
                max_soc    : float, range 0 .. 1
                    battery SOC should not exceed max_soc
                bat_forecast : float
                    have/need, where 'have' is expected remaining PV power which can be used for battery charging
                    and 'need' is power needed to charge battery to 'max_soc' [W]
        """
        self.pvforecast           = _pvforecast
        wallbox                   = WallBoxFactory()
        if _pvstatus is None:     # -----------------------------------------------------  we need get life PV status data
            pvmonitor             = PVMonitorFactory()
            self.inverter         = pvmonitor.getPVMonitor(self.monitorProvider, self.config)
            self.pvstatus         = self.inverter.getStatus()
            self.wallbox          = wallbox.getWallBox(self.wallboxProvider, self.config)
            self.wallbox.readWB(self.persist['charge_completed'])
            if self.wallbox.status is not None:
                ctrl_power        = self._I_to_P(self.wallbox.status['ctrl_current'])
                if self.I_min is None or self.I_min < self.wallbox.status['I_min']: self.I_min = self.wallbox.status['I_min']
                if self.I_max is None or self.I_max > self.wallbox.status['I_max']: self.I_max = self.wallbox.status['I_max']
            else:
                ctrl_power        = self.persist['ctrl_power']                           # fall-back
            self.currTime         = self.pvstatus.name
            if self.pvforecast is None:
                forecastObj       = PVForecast(self.config)
                self.pvforecast   = forecastObj.getForecast(self.pvstatus.name)
            active                = True                                                 # we are in active mode, actually controlling wallbox

        else:                     # ----------------------------------------------------- running in simulation mode
            self.pvstatus         = _pvstatus
            self.wallbox          = wallbox.getWallBox('dummy', self.config)
            self.wallbox.status   = _carstatus
            self.currTime         = self.pvstatus.name                                   # time of last PV status
            ctrl_power            = self.persist['ctrl_power'] 
            if self.I_min is None: self.I_min =  6                                       # fall-back values
            if self.I_max is None: self.I_max = 16
            active                = False

        self.I_min                = self.wallbox.round_current(self.I_min)
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
        self._getI_charge(ctrl_power)                                                    # calculate WB charge current
        fastcharge                       = self._manageBatCharge(ctrl_power)             # calculate max. charge battery power

        self.ctrlstatus['I_charge']      = self.I_charge
        self.ctrlstatus['fastcharge']    = fastcharge
        self.persist['ctrl_power']       = self._I_to_P(self.I_charge)                   # prepare persistent data structure - will be written to file in __del__
        self.persist['charge_completed'] = self.wallbox.status['charge_completed']
        self.persist['saved']            = self.currTime
        
        if active:                                                                       # actively controll wallbox
            self._logInflux()
            self.wallbox.controlWB(self.I_charge)
            if self.inverter is not None:
                self.inverter.setBatCharge(fastcharge, self.feedInLimit, self.maxBatCharge, self.maxSOC)
                del self.inverter
        self.ctrlstatus['ctrl_power'] = self._I_to_P(self.I_charge)
        self.ctrlstatus['max_soc']    = self.maxSOC
        return(self.ctrlstatus)

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
        if self.pvstatus is None: startSOC = 0
        else:                     startSOC = self.pvstatus.soc
        t            = datetime(1970, 1, 1, 0, 0, tzinfo=pytz.utc)
        self.persist = { 'saved'            : t,                                         # time stamp of persistent data
                         'ctrl_power'       : 0,                                         # power delivered to controller in prior step (for sim, as fall-back)
                         'overflow_end'     : time(0, 0),                                # end of time period for potential 70% power limitation
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
            times       = pd.date_range(self.currTime, self.currTime.replace(hour=23), freq="15min")
            times_df    = pd.DataFrame(times).set_index(0)
            clearsky    = myPVSystem.runModel(times_df, 'clearsky')
            endcharge   = {}  
            for I in range(1, math.ceil(self.I_max), 1):
                P       = self._I_to_P(I)
                power   = clearsky.loc[clearsky['dc_clearsky'] > P/self.InverterEff]
                if not power.empty:                                                      # at what time do we have last time enough clearsky power to generate current I?
                    endcharge[I] = power.iloc[-1].name.time()
            self.persist['endcharge'] = endcharge
            power       = clearsky.loc[clearsky['dc_clearsky'] > self.feedInLimit/self.InverterEff]
            if power.empty:
                overflow_end = time(0, 0)                                                # no power limit period
            else:
                overflow_end = (power.iloc[-1].name + timedelta(minutes=30)).time()      # give 30min slack for over-radiation
            self.persist['overflow_end'] = overflow_end
            print('power_limit_ends for ' + str(self.currTime.date()) + ': ' + str(overflow_end))
        return()

    def _getI_charge(self, ctrl_power):
        """
        Determine current for EV excess charging. This method is the core of the smart PV excess charging algorithm
        
        Parameters
        ----------
            ctrl_power : float
                power currently delivered by wallbox (based on calculations from previous time stamp)
        """
        if self.wallbox.status['connected']:
            I_prev                = self._P_to_I(ctrl_power)                             # what we have been charging so far
            if abs(self.I_min - I_prev) < 0.1:                                           # we suffer from rounding errors
                I_prev            = self.I_min
            avail_P               = self.pvstatus.dc_power*self.InverterEff - self.pvstatus.home_consumption + ctrl_power
            if avail_P < 0: avail_P = 0                                                  # negative: no PV power available at all
            I_maxPV               = self._P_to_I(avail_P)
            I_missing             = 0
            if ctrl_power > 0  and I_maxPV < self.I_min:                                 # if we can supply that much power, we are mid-way between previous and min
                I_missing         = (I_prev + self.I_min)/2 - I_maxPV 
            if ctrl_power == 0 and I_maxPV + self.I_gridMax > self.I_min:                # try to harvest battery and grid
                I_missing         = self.I_min - I_maxPV
            if I_missing > 0:
                I_bat             = self._maxFromBat(self.coeff_A)                       # current we can supply, using Coeff_A
                if I_missing > I_bat:                                                    # we don't want provide so much from the battery
                    I_missing     = self.I_min - I_maxPV                                 # if at least we can get this, we can continue with charging
                    I_bat         = self._maxFromBat(self.coeff_B)                       # max. avail current based on coeff. b1, b2 to sustain I_min
                    if I_missing > I_bat + self.I_gridMax:                               # we can't supply from battery alone, or battery plus grid allowence
                        I_missing = 0
                I = math.floor(self.I_min - self.I_gridMax)                              # will not be able to charge anymore without battery
                if I in self.persist['endcharge']:
                    t = self.persist['endcharge'][I]
                    if self.currTime.time() > t:
                        I_missing = 0
                I_charge          = I_maxPV + I_missing                                  # how much we want supply - this may include some grid power
                if I_prev > 0 and I_charge > I_prev: I_charge = I_prev                   # .. this should only be due to rounding errors
            else:  I_charge       = I_maxPV
            I_charge = self.wallbox.round_current(I_charge)                              # HardyBarth rounds down to full amps

            if I_charge < self.I_min: I_charge = 0                                       # we are below the limit which WB can deliver for charging
            if I_charge > self.I_max: I_charge = self.I_max                              # we can't charge with more current than this
            self.I_charge         = I_charge
        else:
            self.I_charge         =  0
            avail_P               = -1
        self.ctrlstatus['avail_P'] = avail_P
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

            if need > have/self.coeff_C[0]:                                              # oops - we should start focusing on battery now
                fastcharge      = True
                self.I_charge   = self.I_charge - self._P_to_I(self.maxBatCharge)        # stop charging car
                if self.I_charge < self.I_min: self.I_charge = 0
            elif self.wallbox.status['connected'] and not self.wallbox.status['charge_completed']:        # planning to / ongoing car charge - all surplus goes to battery
                fastcharge      = True
                if need < have/self.coeff_C[1] and self.currTime.time() < self.persist['overflow_end']:   # don't charge full yet, whilst charging car
                    self.maxSOC = self.maxSOCCharge
            elif need > have/self.coeff_C[1] and self.currTime.time() < self.persist['overflow_end']:     # still early, but not that much more energy left ...
                fastcharge      = True
            elif self.currTime.time() > self.persist['overflow_end']:                    # afternoon - charge battery now without further condition
                fastcharge      = True
        self.ctrlstatus['need'] = need
        self.ctrlstatus['have'] = have
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
        if self.pvstatus.soc > self.minSOC:
            I_bat = self.pvstatus.soc*a+b                                                # max. avail current based on coeff. a1, a2 to slowly reduce charing
        else:
            I_bat = 0    
        return(I_bat)

    def _logInflux(self):
        """
        Log controller information to Influx. Three measurements are created:

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
                have         : forecasted excess PV power for rest of day
                need         : needed power to charge home battery to maxSOC
                bat_forecast : have/need
                calc_soc     : calculated home battery SOC based on battery currents and voltages
                fastcharge   : fast-charge (or smart-charge) home battery
        """
        
        host = self.config['PVControl'].get('host', None)
        if host is not None:
            try:
                port     = self.config['PVControl'].getint('port', 8086)
                database = self.config['PVControl'].get('database')
                client   = DataFrameClient(host=host, port=port, database=database)
                df       = pd.DataFrame(self.wallbox.status, index = [self.currTime])
                df.drop(['I_min', 'I_max'], axis=1, inplace=True)
                for field in df:
                    df.loc[:,field] = df[field].astype(float)
                client.write_points(df, 'wbstatus')

                df       = pd.DataFrame(self.pvstatus.to_frame().transpose())
                for field in df:
                    df.loc[:,field] = df[field].astype(float)
                client.write_points(df, 'pvstatus')

                df       = pd.DataFrame(self.ctrlstatus, index = [self.currTime])
                for field in df:
                    df.loc[:,field] = df[field].astype(float)
                client.write_points(df, 'ctrlstatus')
            except Exception as e:
                print('pvcontrol._logInflux: ' + str(e))
