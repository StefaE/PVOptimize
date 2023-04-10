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

from astral import LocationInfo
from astral.sun import sun
from datetime import datetime, timedelta, time
import pandas as pd
import random

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from .pvcontrol             import PVControl
from .pvmodel.pvmodel       import PVModel
from .pvmonitorfactory      import PVMonitorFactory
from .pvforecast.pvforecast import PVForecast

class PVServer:
    """
    Simulate a rooftop PV system with battery, based on measured data from Solaranzeige

    Methods
    -------
        getDayData(day)
            get data for day 'day' from SolarAnzeige and store in self.pvData; 
            later we'll iterate over each timestamp returned with iterator _dataGen()
        runController()
            run and simulate controller for the day for which previously getDayData(day)
            was called
        plot(hasCtrl)
            plot simulation results. If hasCtrl = False, controller was not run and only
            raw data from SolarAnzeige are to be plotted
    """

    def __init__(self, config):
        self.config             = config
        self.pvsystem           = PVModel(self.config)                                                       # Model of PV system (for clearsky performance calculation)
        pvmonitor               = PVMonitorFactory()
        self.pvmonitor          = pvmonitor.getPVMonitor('SolarAnzeige', self.config)
        self.pvData             = None                                                                       # data from PV (Solaranzeige) - current day only
        self.ctrlData           = None                                                                       # simulated data              - current day only
        self.currTime           = None                                                                       # current simulation time step
        self.day                = None                                                                       # current day

        startDate               = datetime.strptime(self.config['PVServer'].get('startDate'),          '%Y-%m-%d')
        endDate                 = datetime.strptime(self.config['PVServer'].get('endDate', startDate), '%Y-%m-%d')
        delta                   = endDate - startDate
        self.days               = [startDate + timedelta(days=x) for x in range(delta.days+1)]               # days to iterate over 

        self._startTime         = self._time(self.config['PVServer'].get('startTime', None))
        self._endTime           = self._time(self.config['PVServer'].get('endTime', None))
        self._breakTime         = self._time(self.config['PVServer'].get('breakTime', None))

        self.minSOC             = self.config['PVStorage'].getfloat('minSOC', 0.05)                          # minSOC level, below which discharging is inhibited
        self.batCapacity        = self.config['PVStorage'].getint('batCapacity')                             # battery capacity (Wh)
        self.maxBatDischarge    = self.config['PVStorage'].getint('maxBatDischarge')                         # maximum battery discharge power (W)
        self.maxBatCharge       = self.config['PVStorage'].getint('maxBatCharge', self.maxBatDischarge)      # maximum battery charge    power (W)

        self.startSOC           = self.config['PVServer'].getfloat('startSOC', -1)                           # SOC level assumed at sunrise, -1: take from database
        self.emergencyCharge    = self.config['PVServer'].getfloat('emergencyCharge', 0.03)                  # level where emergency charging starts
        self.InverterEff        = 0.97                                                                       # nominal inverter efficiency

        self.maxConsumption     = self.config['PVServer'].getint('maxConsumption', 99999)                    # consumption above this value is removed (default 99999 inhibits removal)
        self.baseConsumption    = self.config['PVServer'].getint('baseConsumption',  350)                    # ... replaced with baseConsume
        self.sigmaConsumption   = self.config['PVServer'].getint('sigmaConsumption',  50)                    # ... +- sigmaConsume; no jittering if zero --> reproducible results

        self.feedInLimit        = self.config['PVServer'].getint('feedInLimit', 99999)

        connectTime             = self.config['PVServer'].get('connectTime', None)                           # when we connect car for charging
        if connectTime is not None:
            h, m                = connectTime.split(':')
            self._connectTime   = time(int(h), int(m))
        else: self._connectTime = None
        self.chargePower        = self.config['PVServer'].getint('chargePower', 16000)                       # how much we want to charge (Wh)

    def _time(self, timeStr):
        if timeStr is not None:
            h, m = timeStr.split(':')[:2]
            t    = time(int(h), int(m))
        else: t  = None
        return(t)

    def _getDaylight(self, day: datetime):
        """
        Get sunrise/sunset - we only simulate during daylight
        """
        latitude  = self.config['PVSystem'].getfloat('Latitude')
        longitude = self.config['PVSystem'].getfloat('Longitude')
        location  = LocationInfo('na', 'na', 'UTC', latitude=latitude, longitude=longitude)
        mySun     = sun(location.observer, date=day)
        sunrise   = mySun['sunrise'].strftime('%Y-%m-%dT%H:%M:%SZ')
        sunset    = mySun['sunset'].strftime('%Y-%m-%dT%H:%M:%SZ')
        return(sunrise, sunset)

    def _dataGen(self):
        """
        row generator over self.pvData; row[] represents data for 'current' timestamp (minute)
        self.currTime          reflects time stamp of current row
        myRow                  returns data for time stamp. home_consumption is modifed so
                               that existing EV charging consumption is filtered out
        """
        for index, row in self.pvData.iterrows():
            self.currTime = index
            myRow         = row.copy()
            if (myRow['home_consumption'] > self.maxConsumption):
                if self.sigmaConsumption > 0:
                    myRow['home_consumption'] = random.gauss(self.baseConsumption, self.sigmaConsumption)
                else:
                    myRow['home_consumption'] = self.baseConsumption
            yield myRow

    def getDayData(self, day: datetime):
        """
        Get one day worth of data from Influx database (Solaranzeige) to self.pvData
        """
        self.day        = day
        sunrise, sunset = self._getDaylight(day)
        if self._startTime is None: startTime = sunrise
        else:                       startTime = day.strftime('%Y-%m-%dT') + self._startTime.strftime('%H:%M:00Z')
        if self._endTime   is None: endTime   = sunset
        else:                       endTime   = day.strftime('%Y-%m-%dT') + self._endTime.strftime('%H:%M:00Z')
        self.pvData     = self.pvmonitor.getStatus(startTime, endTime)
 
        clearsky        = self.pvsystem.runModel(self.pvData, 'clearsky')
        self.pvData     = pd.concat([self.pvData, clearsky['dc_clearsky']], axis = 1)

    def runController(self):
        """
        Simulate the PV system and distribute power to/from battery/grid, etc. Each loop creates
        and eventually distroys an object of Class PVControl. Day summary data are accumulated in
        self.ctrlData for later evaluation in plot()

        PVControl.runControl returns
            ctrl['ctrlstatus] as a dictionary with the following elements (other elements are not used)
                ctrl_power : float (mandatory)
                    total provided by teh controller to wallbox [W]

                fastcharge : boolean (optional, default True)
                    fast-charge home battery, or use smart-charge capability of inverter
                inhibitDischarge : boolean (optional, default False, useful if controller sets inverter discharge rate to zero)
                    inhibit battery discharging
                max_soc    : float, range 0 .. 1 (optional, default 1, useful if controller sets inverter MaxSOC)
                    battery SOC should not exceed max_soc
                bat_forecast : float (optional, used for result plotting only)
                    have/need, where 'have' is expected remaining PV power which can be used for battery charging
                    and 'need' is power needed to charge battery to 'max_soc' [W]

        Other parameters required for simulation are taken from pvstatus

        Note that if self._breakTime is set, a location is reached where a breakpoint can be set
        for easy debugging.
        """

        e             = self.InverterEff
        ctrlResult    = []
        i             =  0
        prevCtrlPower =  0
        prevBatCharge =  0
        forecastObj   = PVForecast(self.config)
        carstatus     = { 'connected'        : 0,
                          'charge_completed' : 0 }
        totCtrlPower  =  0
        for pvstatus in self._dataGen():
            controlObj  = PVControl(self.config)
            pvstatus.home_consumption += prevCtrlPower                                                       # control power of previous step must be added to home consumption

            pvstatus.waste_power = 0
            feedIn = pvstatus.dc_power*e - pvstatus.home_consumption - prevBatCharge                         # current possible feed-in power
            if feedIn > self.feedInLimit:                                                                    # clip
                pvstatus.waste_power = feedIn - self.feedInLimit
                pvstatus.dc_power   -= pvstatus.waste_power                                                  # this is what PV system delivers after clipping
            
            if i > 0: 
                pvstatus.soc       = ctrlResult[i-1]['soc']                                                  # SOC is not taken from database but calculated in previous step
                pvstatus.bat_power = ctrlResult[i-1]['bat_power']
            else:
                if self.startSOC    != -1:                                                                   # -1 (= default): Take whatever the database tells us
                    pvstatus.soc       = self.startSOC
                else:
                    print('start_soc (morning): ' + str(pvstatus.soc))
            currTime               = pvstatus.name
            if self._breakTime is not None and currTime.time() > self._breakTime:
                print('Break time: ' + str(currTime))                                                        # ========== break time evaluation
                pass
            if self._connectTime is not None and currTime.time() > self._connectTime:
                carstatus['connected'] = 1

            pvforecast             = forecastObj.getForecast(currTime)
            ctrl                   = controlObj.runControl(pvstatus, pvforecast, carstatus)                  # ---------- run controller
            ctrl                   = ctrl['ctrlstatus']                                                      # we don't need 'pvstatus', 'wbstatus' in simulation
            if 'ctrl_power' not in ctrl:                                                                     # controller needs some power: ctrl_power
                raise Exception("ERROR --- controller " + controlObj.Name + " does not appear to control any power")
            if ctrl['ctrl_power'] < 0:
                raise Exception("ERROR --- controller " + controlObj.Name + " can only consume, not produce power; " + str(self.currTime))

            if 'dc_power'         not in ctrl: ctrl['dc_power']         = pvstatus.dc_power                  # a controller might potentially re-calculate these elements
            if 'home_consumption' not in ctrl: ctrl['home_consumption'] = pvstatus.home_consumption - prevCtrlPower
            if 'min_soc'          not in ctrl: ctrl['min_soc']          = self.minSOC
            if 'max_soc'          not in ctrl: ctrl['max_soc']          = 1
            if 'fastcharge'       not in ctrl: ctrl['fastcharge']       = True
            if 'inhibitDischarge' not in ctrl: ctrl['inhibitDischarge'] = False
            if 'bat_forecast'     not in ctrl: ctrl['bat_forecast']     = 0
            if totCtrlPower > self.chargePower:
                carstatus['charge_completed'] = 1
                ctrl['ctrl_power'] = 0
            prevCtrlPower          = ctrl['ctrl_power']

            ctrl['soc']            = pvstatus.soc
            ctrl['datetime']       = self.currTime                                                           # ---------- react to controller result
            ctrl['grid_power']     = 0                                                                       # these elements are part of simulator
            ctrl['bat_power']      = 0
            dT                     = 1/60
            if (i > 0): dT         = (self.currTime - ctrlResult[i-1]['datetime']).seconds/3600              # time since last simulation interval, in hours
            ctrl['waste_power']    = pvstatus.waste_power
            
            surpluspower = ctrl['dc_power']*e - ctrl['home_consumption'] - ctrl['ctrl_power'] + pvstatus.waste_power        # available surplus power before battery charge
            if surpluspower > 0:                                                                             # PV provides sufficient power
                if ctrl['soc'] < ctrl['max_soc']:                                                            # SOC < max_soc, charge battery
                    if ctrl['fastcharge']:
                        bat_power = surpluspower
                    else:                                                                                    # only charge battery what would become waste_power
                        bat_power = surpluspower - self.feedInLimit
                    if bat_power < 0: 
                        bat_power = 0
                    elif bat_power > self.maxBatCharge: 
                        bat_power   = self.maxBatCharge                                                      # more PV power available than battery can accept
                else: bat_power     = 0
                ctrl['waste_power'] = surpluspower - bat_power - self.feedInLimit
                if ctrl['waste_power'] < 0: ctrl['waste_power'] = 0
                ctrl['grid_power']  = -(surpluspower - bat_power - pvstatus.waste_power)                                            # gridpower after battery charge
                ctrl['bat_power']   = bat_power                    

            else:                                                                                            # PV provides insufficient power
                if ctrl['soc'] > ctrl['min_soc'] and not ctrl['inhibitDischarge']:                           # battery can serve excess power needed  ---- add: only if battery discharging enabled
                    ctrl['bat_power'] = -(ctrl['ctrl_power'] + ctrl['home_consumption'] - ctrl['dc_power'])  # (discharge: <0)
                    if (abs(ctrl['bat_power']) > self.maxBatDischarge):                                      # consumption exceeding maximum battery discharge power
                        ctrl['grid_power'] = abs(ctrl['bat_power']) - self.maxBatDischarge                   # (consume: >0)
                        ctrl['bat_power']  = -self.maxBatDischarge                                           # ... but it can deliver this much
                else:                                                                                        # battery fully discharged
                    ctrl['grid_power'] = -surpluspower                                                       # (consume: >0)   get all consumption from grid
            prevBatCharge              = ctrl['bat_power']

            ctrl['soc'] = ctrl['soc'] + ctrl['bat_power']*dT/self.batCapacity                                # update SOC
            if ctrl['soc'] > 1:
                excess              = (ctrl['soc'] - 1)*self.batCapacity/dT                                  # we overcharged battery - shift to grid
                ctrl['bat_power']  -= excess
                ctrl['grid_power'] -= excess
                ctrl['soc']         = 1
            elif ctrl['soc'] < self.emergencyCharge and ctrl['bat_power'] <= 0:                              # emergency charge - TO DO: charge to 10%
                ctrl['grid_power'] += self.maxBatCharge + ctrl['bat_power']                                  # charge and compensate for whatever was planned to be used from battery
                ctrl['bat_power']   = self.maxBatCharge
                ctrl['soc']        += self.maxBatCharge*dT/self.batCapacity
            totCtrlPower += ctrl['ctrl_power']/60
  
            ctrlResult.append(ctrl)                                                                          # ---------- build full-day controller result data
            i += 1
            self.ctrlData = pd.DataFrame.from_dict(ctrlResult)
            self.ctrlData.set_index('datetime', inplace=True)
            del  controlObj

    def plot(self, hasCtrl):
        """
        Plot simulation results from self.ctrlData for one day, either interactive or save to .png file

        Parameters
        ----------
            hasCtrl : boolean
            If true, simulator (PVControl.runControl()) was run and four graphs (original data, soc and
            simulation result data, soc) are to be created. If false, only original data, soc graphs are
            created

        Return
        ------
            summary : Pandas Series
                This can be accummulated by the caller into a Pandas DataFrame to eventually
                create a simulation result with one row per simulated day.
        """

        maxY = self.config['PVServer'].getfloat('maxY', 10000) * 1.05
        if (hasCtrl):
            fig, axes = plt.subplots(2, 2, sharex=True, sharey=False, figsize=(20, 11))
        else:
            fig, ax = plt.subplots(2, 1, sharex=True, sharey=False, figsize=(7, 8))
            axes    = [[ax[0], None], [ax[1], None]]                                     # ... so that we can address it same way as above
        idx       = self.pvData.index.values
        datemin   = min(idx)                                                             # axis range
        datemax   = max(idx)
        day     = self.day.strftime('%Y-%m-%d')
        axes[0][0].plot(idx, self.pvData['dc_power'],         label='dc_power')
        axes[0][0].plot(idx, self.pvData['bat_power'],        label='bat_power')
        axes[0][0].plot(idx, self.pvData['grid_power'],       label='grid_power')
        axes[0][0].plot(idx, self.pvData['home_consumption'], label='home_consumption')
        if ('dc_clearsky' in self.pvData):
           axes[0][0].plot(idx, self.pvData['dc_clearsky'],   label='dc_clearsky')
        axes[0][0].axhline(self.feedInLimit, ls='--', linewidth = 0.5, color = 'black')
        axes[0][0].axhline(self.feedInLimit/self.InverterEff + self.baseConsumption, ls='--', linewidth = 0.5, color = 'blue')
        axes[0][0].axhline(-self.feedInLimit, ls='--', linewidth = 0.5, color = 'green')
        axes[1][0].plot(idx, self.pvData['soc']*100,          label='soc')

        axes[0][0].set_ylim([-maxY, maxY])
        axes[1][0].set_ylim([0, 102])
        axes[0][0].set_title(day + ": as reported by Solaranzeige")
        axes[0][0].legend(loc='best')
        axes[1][0].legend(loc='best')

        if hasCtrl:
            idx       = self.ctrlData.index.values
            axes[0][1].plot(idx, self.ctrlData['dc_power'],         label='dc_power')
            axes[0][1].plot(idx, self.ctrlData['bat_power'],        label='bat_power')
            axes[0][1].plot(idx, self.ctrlData['grid_power'],       label='grid_power')
            axes[0][1].plot(idx, self.ctrlData['home_consumption'], label='home_consumption')
            axes[0][1].plot(idx, self.ctrlData['ctrl_power'],       label='ctrl_power')
            axes[0][1].plot(idx, self.ctrlData['waste_power'],      label='waste_power')
            axes[0][1].axhline(-self.feedInLimit, ls='--', linewidth = 0.5, color = 'green')
            axes[1][1].plot(idx, self.ctrlData['bat_forecast']*100, label='bat_forecast%', color='lightgray')
            axes[1][1].plot(idx, self.ctrlData['soc']*100,          label='soc')

            axes[0][1].set_ylim([-maxY, maxY])
            axes[1][1].set_ylim([0, 102])
            axes[0][1].set_title(day + ": as calculated")
            axes[0][1].legend(loc='best')
            axes[1][1].legend(loc='best')
        
            summary                  = self.ctrlData.sum(axis=0)/60
            summary['bat_charge']    = self.ctrlData[self.ctrlData['bat_power']>0]['bat_power'].sum()/60
            summary['bat_discharge'] = self.ctrlData[self.ctrlData['bat_power']<0]['bat_power'].sum()/60

            keepLabels   = [ label for label in summary.index if label in 
                             ['ctrl_power', 'dc_power', 'home_consumption', 'grid_power', 'bat_power', 'waste_power', 'bat_charge', 'bat_discharge']]
            summary      = summary[keepLabels]
            summary.name = self.day
        else:
            summary = None

        plt.subplots_adjust(hspace=0)
        plt.rcParams['axes.grid'] = True

        hours = mdates.HourLocator(interval=1)                                           # format the ticks
        h_fmt = mdates.DateFormatter('%H:%M')
        axes[0][0].xaxis.set_major_locator(hours)
        axes[0][0].xaxis.set_major_formatter(h_fmt)
        axes[0][0].set_xlim(datemin, datemax)

        fig.autofmt_xdate()                                                              # rotate x-labels, manage space
        print('start_soc (evening): ' + str(self.ctrlData['soc'][-1]))

        storePNG = self.config['PVServer'].getboolean('storePNG', False)
        storeCSV = self.config['PVServer'].getboolean('storeCSV', False)

        if not storePNG:
            plt.show()
            if hasCtrl: 
                print(summary)
        else:
            file = self.config['PVServer'].get('storePath') + '/' + day + '.png'
            plt.savefig(file)
            plt.close(fig=fig)
        if storeCSV:
            file = self.config['PVServer'].get('storePath') + '/' + day + '.csv'
            self.ctrlData.to_csv(file)
        return(summary)