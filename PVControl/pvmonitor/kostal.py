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

import json
import requests
import os
import sys

import random
import string
import base64
import hashlib
import hmac
from Crypto.Cipher import AES

import pandas as pd
from .pvmonitortemplate import PVMonitorTemplate

class Kostal (PVMonitorTemplate):
    """
    This class implements communication to a Kostal Plenticore inverter with software version 1.18.
    It requires 'external battery control' enabled (see Readme.md).
    The Kostal REST API is documented at http://<IP of Kostal Inverter>/api/v1/

    Public methods are described in abstract Class PVMonitorTemplate
    Private methods are for authentication and proper talking to REST API
    """ 
    def __init__ (self, config):
        try:
            self.config       = config
            self.status       = None
            self._base_url    = self.config['Kostal'].get('host') + '/api/v1'
            if not self._base_url.startswith('http://'): self._base_url = 'http://' + self._base_url
            self._pass_wd     = self.config['Kostal'].get('passwd')
            self.headers      = {'Content-type': 'application/json', 'Accept': 'application/json'}
            self.verbose      = self.config['Kostal'].getboolean('verbose', False)
            self.inhibitWrite = self.config['Kostal'].getboolean('inhibitWrite', False)
            self._LogMeIn()
        except Exception as e:
            print('Kostal.__init__: ' + str(e))
            sys.exit(1)

    def __del__ (self):
        if 'authorization' in self.headers:
            self._LogMeOut()

    def _LogMeIn(self):
        # this routine is derivative work from:
        # https://stackoverflow.com/questions/59053539/api-call-portation-from-java-to-python-kostal-plenticore-inverter
        # https://github.com/kilianknoll/kostal-RESTAPI
        def randomString(stringLength):
            letters = string.ascii_letters
            return ''.join(random.choice(letters) for serverNounce in range(stringLength))

        try:
            clientNounce = randomString(12)
            clientNounce = base64.b64encode(clientNounce.encode('utf-8')).decode('utf-8')
            
            response     = self._postData("/auth/start", { "username": "user", "nonce": clientNounce })
            serverNounce = response['nonce']
            transID      = response['transactionId']
            rounds       = response['rounds']
            salt64       = base64.b64decode(response['salt'])
            
            saltedPW     = hashlib.pbkdf2_hmac('sha256', self._pass_wd.encode('utf-8'), salt64, rounds)
            clientKey    = hmac.new(saltedPW, "Client Key".encode('utf-8'), hashlib.sha256).digest()
            serverKey    = hmac.new(saltedPW, "Server Key".encode('utf-8'), hashlib.sha256).digest()
            storedKey    = hashlib.sha256(clientKey).digest()
            authMsg      = "n=user,r="+clientNounce+",r="+serverNounce+",s="+response['salt']+",i="+str(rounds)+",c=biws,r="+serverNounce
            clientSign   = hmac.new(storedKey, authMsg.encode('utf-8'), hashlib.sha256).digest()
            serverSign   = hmac.new(serverKey, authMsg.encode('utf-8'), hashlib.sha256).digest()
            f            = bytes(a ^ b for (a, b) in zip(clientKey, clientSign))
            proof        = base64.b64encode(f).decode('utf-8')
            
            response     = self._postData("/auth/finish", { "transactionId": transID, "proof": proof })
            token        = response['token']
            #signature   = response['signature']
            
            serverSign   = hmac.new(storedKey, "Session Key".encode('utf-8'), hashlib.sha256)
            serverSign.update(authMsg.encode('utf-8'))
            serverSign.update(clientKey)
            protocol_key = serverSign.digest()
            t            = os.urandom(16)
            
            e2           = AES.new(protocol_key,AES.MODE_GCM,t)
            e2, authtag  = e2.encrypt_and_digest(token.encode('utf-8'))
            
            step3 = { "transactionId" : transID,
                    "iv"            : base64.b64encode(t).decode('utf-8'),
                    "tag"           : base64.b64encode(authtag).decode("utf-8"),
                    "payload"       : base64.b64encode(e2).decode('utf-8') }
            
            response = self._postData("/auth/create_session", step3)
            self.headers['authorization'] = "Session " + response['sessionId']
        except Exception as e:
            print ("Kostal._LogMeIn: ERROR --- unable to login" + str(e))
            sys.exit(1)
        return()

    def _LogMeOut(self):
        try:
            self._postData("/auth/logout")
            self.headers.pop('authorization', None)
        except Exception as e:
            print ("Kostal._LogMeOut: ERROR --- unable to logout" + str(e))
            sys.exit(1)

    def _getData(self, endpoint):
        try:
            e = endpoint.replace(':', '%3A')
            e = e.replace(',', '%2C')
            r = requests.get(url = self._base_url + e, headers = self.headers)
            if r.reason != 'OK': 
                raise Exception("ERROR --- request to endpoint=" + endpoint + " --- Reason: " + r.reason)
            return(r.json())
        except Exception as e:
            print("Kostal._getData: " + str(e))
            return(None)

    def _postData(self, endpoint, data = None, isPut = False):
        try:
            e = endpoint.replace(':', '%3A')
            if isPut: r = requests.put (url = self._base_url + e, json = data, headers = self.headers)
            else:     r = requests.post(url = self._base_url + e, json = data, headers = self.headers)
            if r.reason != 'OK':
                raise Exception("ERROR --- request to endpoint=" + endpoint + " --- Reason: " + r.reason)
            return(r.json())
        except Exception as e:
            print("ERROR -- Kostal._postData: " + str(e))
            return(None)

    def _setSetting(self, key, value):
        if not self.inhibitWrite:
            data = [ {"settings" : [ {"id":key, "value":str(value)} ], "moduleid":"devices:local"} ]
            self._postData('/settings', data, isPut=True)
        if self.verbose:
            print('Kostal._setSetting: ' + str(key) + " = " + str(value))
        return()

    def getStatus(self):
        """
        Get status from PV System

        Returns
        -------
            Pandas Series with mandatory fields as described in abstract Class PVMonitorTemplate.
            Additionally, following fields are read out:
                grid power       : power consumed (>0) from / delivered to (<0) grid 
                feedinLimit      : power limit for grid feed-in (for Kostal, this includes home consumption)
                max_bat_charge   : maximum allowed battery charge power [W]
                max_soc          : maximum allowed SOC (0 .. 1)
                smart_bat_ctrl   : smart battery control enabled / disabled
        """
        status                     = {}
        data                       = self._getData('/processdata/devices:local/Home_P,Grid_P,LimitEvuAbs')[0]['processdata']
        status['home_consumption'] = [elem['value'] for elem in data if elem['id'] == 'Home_P'][0]
        status['grid_power']       = [elem['value'] for elem in data if elem['id'] == 'Grid_P'][0]
        status['feedinLimit']      = [elem['value'] for elem in data if elem['id'] == 'LimitEvuAbs'][0]
        status['dc_power']         = self._getData('/processdata/devices:local:pv1/P')[0]['processdata'][0]['value']
        status['dc_power']        += self._getData('/processdata/devices:local:pv2/P')[0]['processdata'][0]['value']
        if status['dc_power'] < 0: status['dc_power'] = 0
        data                       = self._getData('/processdata/devices:local:ac/L1_U,L2_U,L3_U')[0]['processdata']
        status['grid_voltage']     = sum([elem['value'] for elem in data])/3
        data                       = self._getData('/processdata/devices:local:battery/P,SoC,LimitEvuAbs')[0]['processdata']
        status['bat_power']        = [elem['value'] for elem in data if elem['id'] == 'P'][0]
        status['soc']              = [elem['value'] for elem in data if elem['id'] == 'SoC'][0]/100
        data                       = self._getData('/settings/devices:local/Battery:ExternControl:MaxChargePowerAbs,Battery:ExternControl:MaxSocRel,Battery:SmartBatteryControl:Enable')
        status['max_bat_charge']   = float([elem['value'] for elem in data if elem['id'] == 'Battery:ExternControl:MaxChargePowerAbs'][0])      # strangely, returns string
        status['max_soc']          = float([elem['value'] for elem in data if elem['id'] == 'Battery:ExternControl:MaxSocRel'][0])/100          # strangely, returns string
        status['smart_bat_ctrl']   = int([elem['value'] for elem in data if elem['id'] == 'Battery:SmartBatteryControl:Enable'][0])             # strangely, returns string

        status                     = pd.Series(status, name = pd.Timestamp.utcnow())
        self.status                = status
        return(status)

    def setBatCharge(self, fastcharge, feedinLimit, maxChargeLim, maxSoc = 1):
        try:
            max_charge = None
            if self.status is not None:
                if self.status['dc_power'] > 50:                                         # only set battery charge strategy if we have any dc_power
                    if fastcharge:
                        if self.status['smart_bat_ctrl']:
                            self._setSetting("Battery:SmartBatteryControl:Enable", 0)
                    else:
                        if not self.status['smart_bat_ctrl']:
                            self._setSetting("Battery:SmartBatteryControl:Enable", 1)

                        max_charge = None
                        if self.status['grid_power'] < 0 and self.status['grid_power'] > -feedinLimit*0.9 and self.status['bat_power'] < 20:
                            max_charge = self.status['max_bat_charge'] / 2               # exponentially reduce max_charge
                            if max_charge < 200: max_charge = 0
                        elif self.status['grid_power'] < -feedinLimit*0.9 and self.status['max_bat_charge'] < maxChargeLim * 0.9:
                            max_charge = maxChargeLim*1.05                               # essentially disable max_charge, fall back to default

                        if max_charge is not None:
                            self._setSetting("Battery:ExternControl:MaxChargePowerAbs", max_charge)
                if maxSoc < 1:
                    self._setSetting("Battery:ExternControl:MaxSocRel", maxSoc*100)
                    if max_charge is None and self.status['max_bat_charge'] < maxChargeLim * 0.9:
                        self._setSetting("Battery:ExternControl:MaxChargePowerAbs", maxChargeLim*1.05)
            else:
                raise Exception ("ERROR - Kostal status not initialized")
        except Exception as e:
            print("ERROR -- Kostal.setBatCharge: " + str(e))
        return()
        
