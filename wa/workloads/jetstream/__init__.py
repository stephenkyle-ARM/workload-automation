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
from http.server import SimpleHTTPRequestHandler, HTTPServer
import logging
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid

from wa import Parameter, Workload, File
from wa.framework.exception import WorkloadError
from wa.utils.exec_control import once

from devlib.utils.android import adb_command


class Jetstream(Workload):

    name = "jetstream"
    description = """
    A workload to execute the Jetstream 2.0 web based benchmark. Requires device to be rooted.

    Test description:
    1. Host a local copy of the Jetstream website, and make it visible to the device via ADB.
    2. Open chrome via an intent to access the local copy.
    3. Execute the benchmark - appending ?report=true to the URL runs it automatically.
    4. The benchmark will write to the browser's sandboxed local storage to signal the benchmark
       has completed. This local storage is monitored by this workload.

    Known working chrome version 83.0.4103.106

    To modify the archived jetstream workload:

    1. Run 'git clone https://github.com/WebKit/webkit'

    2. Copy PerformanceTests/JetStream2 to a directory called document_root

    3. Modify document_root/JetStream2/JetstreamDriver.js
      3a. Change any checking for equality with '?report=true' to use string.includes() instead.
      3b. Add the listed code after this line (roughly line 270):

            statusElement.innerHTML = '';

          Code to add:

			if (location.search.length > 1) {
				var parts = location.search.substring(1).split('&');
				for (var i = 0; i < parts.length; i++) {
					var keyValue = parts[i].split('=');
					var key = keyValue[0];
					var value = keyValue[1];
					if (key === "reportEndId") {
						window.localStorage.setItem('reportEndId', value);
					}
				}
			}

    5. Run 'tar -cpzf jetstream_archive.tgz document_root'

    6. Copy the tarball into the workloads/jetstream directory

    7. If appropriate, update the commit info in the LICENSE file.
    """
    supported_platforms = ["android"]

    package_names = ["org.chromium.chrome", "com.android.chrome"]
    regex = re.compile('node index="0" text="(\d+.\d+)" resource-id=""')

    parameters = [
        Parameter(
            "chrome_package",
            allowed_values=package_names,
            kind=str,
            default="com.android.chrome",
            description="""
                  The app package for the browser that will be launched.
                  """,
        ),
    ]

    @once
    def initialize(self, context):
        super(Jetstream, self).initialize(context)
        self.archive_server = ArchiveServer()
        if not self.target.is_rooted:
            raise WorkloadError(
                "Device must be rooted for the jetstream workload currently"
            )

        # Temporary directory used for storing the Jetstream files, uiautomator
        # dumps, and modified XML chrome config files.
        self.temp_dir = tempfile.TemporaryDirectory()
        self.document_root = os.path.join(self.temp_dir.name, "document_root")

        # Host a copy of Jetstream locally
        tarball = context.get_resource(File(self, "jetstream_archive.tgz"))
        with tarfile.open(name=tarball) as handle:
            handle.extractall(self.temp_dir.name)
        self.archive_server.start(self.document_root, self.target)
        self.webserver_port = self.archive_server.get_port()

        self.jetstream_url = "http://localhost:{}/JetStream2/index.html?report=true".format(
            self.webserver_port
        )

    def setup(self, context):
        super(Jetstream, self).setup(context)

        # We are making sure we start with a 'fresh' browser - no other tabs,
        # nothing in the page cache, etc.

        # Clear the application's cache.
        self.target.execute("pm clear {}".format(self.chrome_package), as_root=True)

        # Launch the browser for the first time and then stop it. Since the
        # cache has just been cleared, this forces it to recreate its
        # preferences file, that we need to modify.
        browser_launch_cmd = "am start -a android.intent.action.VIEW -d {} {}".format(
            self.jetstream_url, self.chrome_package
        )
        self.target.execute(browser_launch_cmd)
        time.sleep(1)
        self.target.execute("am force-stop {}".format(self.chrome_package))
        time.sleep(1)

        # Pull the preferences file from the device, modify it, and push it
        # back.  This is done to bypass the 'first launch' screen of the
        # browser we see after the cache is cleared.
        self.preferences_xml = "{}_preferences.xml".format(self.chrome_package)

        file_to_modify = "/data/data/{}/shared_prefs/{}".format(
            self.chrome_package, self.preferences_xml
        )

        self.target.pull(file_to_modify, self.temp_dir.name, as_root=True)

        with open(os.path.join(self.temp_dir.name, self.preferences_xml)) as read_fh:
            lines = read_fh.readlines()

            # Add additional elements for the preferences XML to the
            # _second-last_ line
            for line in [
                '<boolean name="first_run_flow" value="true" />\n',
                '<boolean name="first_run_tos_accepted" value="true" />\n',
                '<boolean name="first_run_signin_complete" value="true" />\n',
                '<boolean name="displayed_data_reduction_promo" value="true" />\n',
            ]:
                lines.insert(len(lines) - 1, line)

            with open(
                os.path.join(self.temp_dir.name, self.preferences_xml + ".new"), "w",
            ) as write_fh:
                for line in lines:
                    write_fh.write(line)

        # Make sure ownership of the original file is preserved.
        user_owner, group_owner = self.target.execute(
            "ls -l {}".format(file_to_modify), as_root=True,
        ).split()[2:4]

        self.target.push(
            os.path.join(self.temp_dir.name, self.preferences_xml + ".new"),
            file_to_modify,
            as_root=True,
        )

        self.target.execute(
            "chown {}.{} {}".format(user_owner, group_owner, file_to_modify),
            as_root=True,
        )

    def run(self, context):
        super(Jetstream, self).run(context)

        # Generate a UUID to search for in the browser's local storage to find out
        # when the workload has ended.
        report_end_id = uuid.uuid4().hex
        url_with_unique_id = "{}&reportEndId={}".format(
            self.jetstream_url, report_end_id
        )

        browser_launch_cmd = "am start -a android.intent.action.VIEW -d '{}' {}".format(
            url_with_unique_id, self.chrome_package
        )
        self.target.execute(browser_launch_cmd)

        self.wait_for_benchmark_to_complete(report_end_id)

    def wait_for_benchmark_to_complete(self, report_end_id):
        local_storage = "/data/data/{}/app_chrome/Default/Local Storage/leveldb".format(
            self.chrome_package
        )

        sleep_period_s = 5
        find_period_s = 30
        timeout_period_m = 15

        iterations = 0
        local_storage_seen = False
        benchmark_complete = False
        while not benchmark_complete:
            if self.target.file_exists(local_storage):
                if (
                    iterations % (find_period_s // sleep_period_s) == 0
                    or not local_storage_seen
                ):
                    # There's a chance we don't see the localstorage file immediately, and there's a
                    # chance more of them could be created later, so check for those files every ~30
                    # seconds.
                    find_cmd = '{} find "{}" -iname "*.log"'.format(
                        self.target.busybox, local_storage
                    )
                    candidate_files = self.target.execute(find_cmd, as_root=True).split(
                        "\n"
                    )

                local_storage_seen = True

                for ls_file in candidate_files:
                    # Each local storage file is in a binary format. Depending on the grep you use, it
                    # might print out the line '[KEY][VALUE]' or it might just say 'Binary file X
                    # matches' so handle both just in case.
                    grep_cmd = '{} grep {} "{}"'.format(
                        self.target.busybox, report_end_id, ls_file
                    )
                    output = self.target.execute(
                        grep_cmd, as_root=True, check_exit_code=False
                    )
                    if "Binary file {} matches" in output or report_end_id in output:
                        benchmark_complete = True
                        break

            iterations += 1

            if iterations > ((timeout_period_m * 60) // sleep_period_s):
                # We've been waiting 15 minutes for Jetstream to finish running - give up.
                if not local_storage_seen:
                    raise WorkloadError(
                        "Jetstream did not complete within 15m - Local Storage wasn't found"
                    )
                raise WorkloadError("Jetstream did not complete within 15 minutes.")

            time.sleep(sleep_period_s)

    def read_score(self):
        self.target.execute(
            "uiautomator dump {}".format(self.ui_dump_loc), as_root=True
        )
        self.target.pull(self.ui_dump_loc, self.temp_dir.name)

        with open(os.path.join(self.temp_dir.name, "ui_dump.xml"), "rb") as fh:
            dump = fh.read().decode("utf-8")
        match = self.regex.search(dump)
        result = None
        if match:
            result = float(match.group(1))

        return result

    def update_output(self, context):
        super(Jetstream, self).update_output(context)

        self.ui_dump_loc = os.path.join(self.target.working_directory, "ui_dump.xml")

        score_read = False
        iterations = 0
        while not score_read:
            score = self.read_score()

            if score is not None:
                context.add_metric(
                    "Jetstream Score", score, "Runs per minute", lower_is_better=False
                )
                score_read = True
            else:
                if iterations >= 10:
                    raise WorkloadError(
                        "The Jetstream workload has failed. No score was obtainable."
                    )
                else:
                    # Sleep and retry...
                    time.sleep(2)
                    iterations += 1

    def teardown(self, context):
        super(Jetstream, self).teardown(context)

        # The browser's processes can stick around and have minor impact on
        # other performance sensitive workloads, so make sure we clean up.
        self.target.execute("am force-stop {}".format(self.chrome_package))

        if self.cleanup_assets:
            # The only thing left on device was the UI dump created by uiautomator.
            self.target.execute("rm {}".format(self.ui_dump_loc), as_root=True)

    @once
    def finalize(self, context):
        super(Jetstream, self).finalize(context)

        # Shutdown the locally hosted version of Jetstream
        self.archive_server.stop(self.target)


class ArchiveServerThread(threading.Thread):
    """Thread for running the HTTPServer"""

    def __init__(self, httpd):
        self._httpd = httpd
        threading.Thread.__init__(self)

    def run(self):
        self._httpd.serve_forever()


class DifferentDirectoryHTTPRequestHandler(SimpleHTTPRequestHandler):
    """A version of SimpleHTTPRequestHandler that allows us to serve
    relative files from a different directory than the current one.
    This directory is captured in |document_root|. It also suppresses
    logging."""

    def translate_path(self, path):
        document_root = self.server.document_root
        path = SimpleHTTPRequestHandler.translate_path(self, path)
        requested_uri = os.path.relpath(path, os.getcwd())
        return os.path.join(document_root, requested_uri)

    # Disable the logging.
    # pylint: disable=redefined-builtin
    def log_message(self, format, *args):
        pass


class ArchiveServer(object):
    def __init__(self):
        self._port = None

    def start(self, document_root, target):
        # Create the server, and find out the port we've been assigned...
        self._httpd = HTTPServer(("", 0), DifferentDirectoryHTTPRequestHandler)
        # (This property is expected to be read by the
        #  DifferentDirectoryHTTPRequestHandler.translate_path method.)
        self._httpd.document_root = document_root
        _, self._port = self._httpd.server_address

        self._thread = ArchiveServerThread(self._httpd)
        self._thread.start()

        adb_command(target.adb_name, "reverse tcp:{0} tcp:{0}".format(self._port))

    def stop(self, target):
        adb_command(target.adb_name, "reverse --remove tcp:{}".format(self._port))

        self._httpd.shutdown()
        self._thread.join()

    def get_port(self):
        return self._port
