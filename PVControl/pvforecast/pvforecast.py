import pandas as pd
import sys
from influxdb import InfluxDBClient
from datetime import datetime, date, time, timezone, timedelta

class PVForecast:
    def __init__(self, config):
        self.config      = config
        self._host       = self.config['PVForecast'].get('host')
        self._port       = self.config['PVForecast'].getint('port', 8086)
        self._database   = self.config['PVForecast'].get('database')
        self.useForecast = self.config['PVForecast'].getboolean('useForecast', True)
        self.forecast    = None
        self.date        = None
        pass

    def getForecast(self, now):
        """
        Get PV power forecast from Influx database (likely filled from PVForecast project)

        Parameters
        ----------
            now : timestamp, UTC

        Returns
        -------
            self.forecast : Pandas DataFrame with columns
                index     : time stamps, for period_end of respective forecast period
                forecast  : PV power [kWh]
                remain    : sum of remaining forecasted PV power from current time stamp to end of the day
        """
        if not self.useForecast: return(None)
        try:
            if self.date != now.date():
                startTime      = now.strftime('%Y-%m-%dT00:00:00Z')
                endTime        = now.strftime('%Y-%m-%dT23:59:59Z')
                meas, field    = self.config['PVForecast'].get('forecastField').split('.')

                client         = InfluxDBClient(host=self._host, port=self._port, database=self._database)
                sql            = 'SELECT "' + field +'" AS "forecast" FROM "' + meas + '" WHERE time >= ' + "'" + startTime + "' AND time < '" + endTime + "' ORDER BY time DESC"
                select         = client.query(sql)
                forecastDict   = []
                hasData        = False
                total          = 0
                t_end          = None
                duration       = None
                for row in select.get_points():
                    hasData    = True                                                    # if we don't get here, most likely 'power_field' was wrongly configured
                    period_end = row['time'].replace("Z", "+00:00")                      # Influx returns period_start, so we need add 5min
                    if t_end is None: 
                        t_end = datetime.strptime(row['time'], '%Y-%m-%dT%H:%M:%SZ')
                    else:
                        if duration is None:
                            t_start  = datetime.strptime(row['time'], '%Y-%m-%dT%H:%M:%SZ')
                            duration = round((t_end - t_start).total_seconds()/60)     # forecast period in minutes, rounded to minute
                            duration = duration/60
                    avail      = (row['forecast'])
                    if avail < 0: avail = 0
                    forecastDict.append( { 'period_end' : pd.to_datetime(period_end),
                                           'forecast'   : avail,
                                           'remain'     : total } )
                    total     += avail
                if not hasData:
                    raise Exception("ERROR --- no forecast data found")
                self.forecast = pd.DataFrame.from_dict(forecastDict).iloc[::-1]
                self.forecast['forecast'] = self.forecast['forecast'] * duration         # scale kW --> kWh
                self.forecast['remain']   = self.forecast['remain']   * duration
                self.date     = now.date()
            return(self.forecast)
        except Exception as e:
            print("getForecast: " + str(e))
            return(None)
