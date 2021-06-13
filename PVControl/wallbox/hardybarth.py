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

The software controls hardware (Inverter, Wallbox, ...) over APIs and hence 
can potentially cause harm to the hardware and its environment. 
The author cannot be held liable for any damage caused through its use.
** Use at your own risk! **
'''

from .wbtemplate import WBTemplate
import requests

class HardyBarth(WBTemplate):
    """
    Implementation of abstract Class WBTemplate for HardyBarth wallbox.
    The HardyBarth REST API is documented at http://<IP of WallBox>/api/v1/doc#/

    See WBTemplate documentation for further details.
    """
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.id           = self.config['HardyBarth'].get('id', 1)
        self.verbose      = self.config['HardyBarth'].getboolean('verbose', False)
        self.inhibitWrite = self.config['HardyBarth'].getboolean('inhibitWrite', False)
        host           = self.config['HardyBarth'].get('host')                           # wallbox address
        self.url       = 'http://' + host + '/api/v1/'

    def readWB(self, charge_completed = False):
        """
        Read status from wallbox. Parameter charge_completed is returned status from previous call to readWB
        
        Parameters
        ----------
            charge_completed : boolean
                value of charge_completed from previous call to readWB()

        Returns
        -------
            Dictionary with mandatory keys as described in abstract Class WBTemplate.
            Additionally, following keys are read out:

            used in method controlWB()
            modeid        : mode  (manual, ECO, ...)
            stateid       : state (idle, charging, ...)
            manualmodeamp : set point for manual mode [A]

            not further used:
            active_power  : active power as measured by wallbox [W]
            current_Lx    : per phase currents for phases x = 1, 2, 3
            cos_phi       : cos(phi) measured
        """
        id   = self.id
        try:
            status               = self._request(False, 'all')
            status               = status.json()['meters'][1]['data']
            data                 = self._request(False, f'chargecontrols/{id}')
            data                 = data.json()['chargecontrol']
            data['ctrl_current'] = data.pop('currentpwmamp')                             # use generic names
            data['I_min']        = data.pop('evminamp')
            data['I_max']        = data.pop('supplylinemaxamp')
            data['active_power'] = status['1-0:1.4.0']
            data['cos_phi']      = status['1-0:13.4.0']
            data['current_L1']   = status['1-0:31.4.0']
            data['current_L2']   = status['1-0:51.4.0']
            data['current_L3']   = status['1-0:71.4.0']
            # manualmodeamp not translated to generic name
            for key in ['id', 'name', 'state', 'mode', 'type', 'version', 'busid', 'vendor']:
                data.pop(key)
            data['charge_completed']     = False
            if data['connected'] and (data['stateid'] == 4 or (data['stateid'] == 17 and charge_completed)): 
                data['charge_completed'] = True
                data['ctrl_current']     = 0                                             # currentpwmamp is still according to last setting
        except Exception as e:
            print('readWB: ' + str(e))
            data = None
        self.status = data
        return()

    def controlWB(self, I_new):
        id = self.id
        if I_new > 0:
            if not self.status['connected']:
                print("Warning --- WB not connected, cannot charge with " + str(I_new))
            else:
                if self.status['modeid'] != 3:                                           # manual
                    self._request(True, f'pvmode', { 'pvmode': 'manual' })
                if self.status['manualmodeamp'] != I_new:
                    self._request(True, f'pvmode/manual/ampere', { 'manualmodeamp': I_new })
                if self.status['stateid'] != 5 and self.status['stateid'] != 4:          # charging / enabled, waiting
                    self._request(True, f'chargecontrols/{id}/start')
        else:
            if self.status['manualmodeamp'] > self.status['I_min']:
                self._request(True, f'pvmode/manual/ampere', { 'manualmodeamp': self.status['I_min'] })
            if self.status['stateid'] != 17 and self.status['stateid'] != 4:             # disabled / enabled, waiting
                self._request(True, f'chargecontrols/{id}/stop')
        return()

    def _request(self, isPost, endpoint = None, data = None):
        msg = ''
        r   = None
        if isPost and not self.inhibitWrite:                                             # we want - and are allowed to - post
            try:
                if endpoint is None: msg = "nothing to do"
                else:
                    if data is None: 
                        msg = "endpoint " + endpoint
                        r = requests.post(self.url + endpoint)
                    else:            
                        key = list(data.keys())[0]
                        msg = "endpoint " + endpoint + ": " + key + " = " + str(data[key])
                        r = requests.post(self.url + endpoint, data)
                    if r.reason != 'OK':
                        raise Exception("ERROR --- request to endpoint=" + endpoint + " --- Reason: " + r.reason)
            except Exception as e:
                print("ERROR -- controlWB - post: " + str(e))
        elif not isPost:                                                                 # we want to 'get' data
            try:
                r = requests.get(self.url + endpoint)
                # msg = "get endpoint " + endpoint
            except Exception as e:
                print("ERROR -- controlWB - get: " + str(e))
        if self.verbose and msg: 
            print("controlWB - Message: " + msg)
        return(r)
