#    Copyright 2014-2018 ARM Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import os
import re
import subprocess
import time

from wa import ApkUiautoWorkload, Parameter, Workload
from wa.framework.exception import ValidationError, WorkloadError
from wa.utils.types import list_of_strs
from wa.utils.misc import unique


class Speedometer(Workload):

    name = 'speedometer'
    package_names = ['com.android.chrome']
    regex = re.compile('text="(\d+.\d+)" resource-id="result-number"')
    versions = ['1.0', '2.0']
    description = '''
    A workload to execute the speedometer web based benchmark

    Test description:
    1. Open chrome
    2. Navigate to the speedometer website - http://browserbench.org/Speedometer/
    3. Execute the benchmark

    known working chrome version 80.0.3987.149
    '''

    parameters = [
        Parameter('speedometer_version', allowed_values=versions, kind=str, default='2.0',
                  description='''
                  The speedometer version to be used.
                  ''')
    ]

    requires_network = True

    def __init__(self, target, **kwargs):
        super(Speedometer, self).__init__(target, **kwargs)

    def setup(self, context):
        super(Speedometer, self).setup(context)
        subprocess.check_output('adb reverse tcp:8000 tcp:8000', shell=True)

    def run(self, context):
        super(Speedometer, self).run(context)
        url = 'am start -a android.intent.action.VIEW -d http://localhost:8000/Speedometer2.0/index.html'
        self.target.execute(url) 

        # Wait 60 seconds at least, and then wait until we don't see the 'sandboxed_process' process for 10 seconds.
        time.sleep(60)

        countdown = 5
        while countdown > 0:
            busiest_line = subprocess.check_output('adb shell top -n1 -m1 -q -b', shell=True).decode('utf-8').split("\n")[0]
            while "sandboxed_process" in busiest_line:
                countdown = 5
                time.sleep(2)
                busiest_line = subprocess.check_output('adb shell top -n1 -m1 -q -b', shell=True).decode('utf-8').split("\n")[0]
            time.sleep(2)
            countdown -= 1

    def teardown(self, context):
        super(Speedometer, self).teardown(context)
        subprocess.check_output('adb reverse --remove tcp:8000', shell=True)

    def update_output(self, context):
        super(Speedometer, self).update_output(context)
        subprocess.check_output("adb shell su -c 'uiautomator dump'", shell=True)
        subprocess.check_output('adb pull /sdcard/window_dump.xml .', shell=True)
        with open('window_dump.xml', 'rb') as fh:
            dump = fh.read().decode('utf-8')
        match = self.regex.search(dump)
        result = None
        if match:
            result = float(match.group(1))

        if result is not None:
            context.add_metric('Speedometer Score', result, 'Runs per minute', lower_is_better=False)
        else:
            raise WorkloadError("The Speedometer workload has failed. No score was obtainable.")
