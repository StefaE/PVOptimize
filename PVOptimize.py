"""
Copyright (C) 2021    Stefan Eichenberger   se_misc ... hotmail.com

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

This is the main script to run PVControl from crontab. This script is 
typically called every minute or so. By default, config.ini in the local 
directory is the configuration file. But argument -c can specify a 
different file.
"""

import configparser
import os
import sys
import argparse
import time
from datetime import datetime, timezone

from PVControl.pvcontrol import PVControl

def get_script_path():
    return os.path.dirname(os.path.realpath(sys.argv[0]))

if __name__ == "__main__":
    cfgParser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cfgParser.add_argument('-c', '--cfg', help="Specify config file (default: ./solcast_light_config.ini)", metavar="FILE")
    args = cfgParser.parse_args()
    if args.cfg: cfgFile = args.cfg
    else:        cfgFile = 'config.ini'

    try:
        myConfig = configparser.ConfigParser(inline_comment_prefixes='#', empty_lines_in_values=False)
        myConfig.read(get_script_path() + '/' + cfgFile)
    except Exception as e:
        print('Error reading config file ' + cfgFile + ': ' + str(e))
        sys.exit(1)

    runDelay    = myConfig['PVControl'].getint('run', 0)                                 # sleep to allow 'solaranzeige' to fully update Influx
    print("-- " + str(datetime.now(timezone.utc)))
    myPVControl = PVControl(myConfig)
    time.sleep(runDelay)
    myPVControl.runControl()
    del myPVControl