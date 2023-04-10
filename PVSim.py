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

This is the main script to run a simulation of PVControl for one or multiple days. 
This script is typically called interactively on a performant machine. By default, 
config.ini in the local directory is the configuration file. But argument -c can
specify a different file.
"""

import configparser
import os
import sys
import argparse
import pandas as pd

from PVControl.pvserver  import PVServer

def get_script_path():
    return os.path.dirname(os.path.realpath(sys.argv[0]))

if __name__ == "__main__":
    cfgParser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cfgParser.add_argument('-c', '--cfg', help="Specify config file (default: ./config.ini)", metavar="FILE")
    args = cfgParser.parse_args()
    if args.cfg: cfgFile = args.cfg
    else:        cfgFile = 'config.ini'
    cfgFile = get_script_path() + '/' + cfgFile

    try:
        myConfig = configparser.ConfigParser(inline_comment_prefixes='#', empty_lines_in_values=False)
        if not os.path.isfile(cfgFile): raise Exception (cfgFile + ' does not exist')
        myConfig.read(cfgFile)
    except Exception as e:
        print('Error reading config file ' + cfgFile + ': ' + str(e))
        sys.exit(1)

    myServer = PVServer(myConfig)                                                        # create PV server to emulate power distribution (bat, grid, ...)
    runCtrl  = myConfig['PVControl'].getint('run', 0)                                    # False doesn't run controller, merely creates plot files for PV system
    if os.path.exists('./pvcontrol.pickle'):
        os.remove('./pvcontrol.pickle')
    summary  = pd.DataFrame()
    for day in myServer.days:                                                            # iterate of startDate .. endDate as defined in config file
        myServer.getDayData(day)                                                         # get data for one day
        if (runCtrl != 0):
            myServer.runController()                                                     # simulate controller (eg. wallbox charging)
        daySummary = myServer.plot(runCtrl)                                              # plot data, summarize
        if daySummary is not None:
            summary = pd.concat([summary, daySummary.to_frame().T])
    if myConfig['PVServer'].getboolean('storePNG') and not summary.empty:
        summary.index.name = 'day'
        file = myConfig['PVServer'].get('storePath') + '/' + 'summary.csv'
        summary.to_csv(file)
    pass