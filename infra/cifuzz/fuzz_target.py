# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A module to handle running a fuzz target for a specified amount of time."""
import logging
import os
import posixpath
import re
import subprocess
import sys
import urllib.request
import zipfile

# pylint: disable=wrong-import-position
# pylint: disable=import-error
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils

# TODO: Turn default logging to WARNING when CIFuzz is stable
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG)

LIBFUZZER_OPTIONS = '-seed=1337 -len_control=0'

# Location of google cloud storage for latest OSS-Fuzz builds.
GCS_BASE_URL = 'https://storage.googleapis.com/clusterfuzz-builds'

# The number of reproduce attempts for a crash.
REPRODUCE_ATTEMPTS = 10

# The name to store the latest OSS-Fuzz build at.
BUILD_STORE_NAME = 'oss_fuzz_latest.zip'


class FuzzTarget:
  """A class to manage a single fuzz target.

  Attributes:
    target_name: The name of the fuzz target.
    duration: The length of time in seconds that the target should run.
    target_path: The location of the fuzz target binary.
    project_name: The name of the relevant OSS-Fuzz project.
  """

  def __init__(self, target_path, duration, out_dir, project_name=None):
    """Represents a single fuzz target.

    Note: project_name should be none when the fuzzer being run is not
    associated with a specific OSS-Fuzz project.

    Args:
      target_path: The location of the fuzz target binary.
      duration: The length of time  in seconds the target should run.
      out_dir: The location of where the output from crashes should be stored.
      project_name: The name of the relevant OSS-Fuzz project.
    """
    self.target_name = os.path.basename(target_path)
    self.duration = duration
    self.target_path = target_path
    self.out_dir = out_dir
    self.project_name = project_name

  def fuzz(self):
    """Starts the fuzz target run for the length of time specified by duration.

    Returns:
      (test_case, stack trace) if found or (None, None) on timeout or error.
    """
    logging.info('Fuzzer %s, started.', self.target_name)
    docker_container = utils.get_container_name()
    command = ['docker', 'run', '--rm', '--privileged']
    if docker_container:
      command += [
          '--volumes-from', docker_container, '-e', 'OUT=' + self.out_dir
      ]
    else:
      command += ['-v', '%s:%s' % (self.out_dir, '/out')]

    command += [
        '-e', 'FUZZING_ENGINE=libfuzzer', '-e', 'SANITIZER=address', '-e',
        'RUN_FUZZER_MODE=interactive', 'gcr.io/oss-fuzz-base/base-runner',
        'bash', '-c', 'run_fuzzer {fuzz_target} {options}'.format(
            fuzz_target=self.target_name, options=LIBFUZZER_OPTIONS)
    ]
    logging.info('Running command: %s', ' '.join(command))
    process = subprocess.Popen(command,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)

    try:
      _, err = process.communicate(timeout=self.duration)
    except subprocess.TimeoutExpired:
      logging.info('Fuzzer %s, finished with timeout.', self.target_name)
      return None, None

    logging.info('Fuzzer %s, ended before timeout.', self.target_name)
    err_str = err.decode('ascii')
    test_case = self.get_test_case(err_str)
    if not test_case:
      logging.error('No test case found in stack trace.', file=sys.stderr)
      return None, None
    if self.is_crash_a_failure(test_case):
      return test_case, err_str
    return None, None

  def is_reproducible(self, test_case, target_path):
    """Checks if the test case reproduces.

      Args:
        test_case: The path to the test case to be tested.
        target_path: The path to the fuzz target to be tested

      Returns:
        True if crash is reproducible.
    """
    command = [
        'docker', 'run', '--rm', '--privileged', '-v',
        '%s:/out' % target_path, '-v',
        '%s:/testcase' % test_case, '-t', 'gcr.io/oss-fuzz-base/base-runner',
        'reproduce', self.target_name, '-runs=100'
    ]
    for _ in range(REPRODUCE_ATTEMPTS):
      _, _, err_code = utils.execute(command)
      if err_code:
        return True
    return False

  def is_crash_a_failure(self, test_case):
    """Checks if a crash is reproducible, and if it is, whether it's a new
    regression that cannot be reproduced with the latest OSS-Fuzz build.

    NOTE: If no project is specified the crash is assumed introduced
    by the pull request if it is reproducible.

    Args:
      test_case: The path to the test_case that triggered the crash.

    Returns:
      True if the crash was introduced by the current pull request.
    """
    reproducible_in_pr = self.is_reproducible(test_case,
                                              os.path.dirname(self.target_path))
    if not self.project_name:
      return reproducible_in_pr

    if not reproducible_in_pr:
      logging.info('Crash is not reproducible.')
      return False

    oss_fuzz_build_dir = self.download_oss_fuzz_build()
    if not oss_fuzz_build_dir:
      return False

    reproducible_in_oss_fuzz = self.is_reproducible(test_case,
                                                    oss_fuzz_build_dir)

    if reproducible_in_pr and not reproducible_in_oss_fuzz:
      logging.info('Crash is new and reproducible.')
      return True
    logging.info('Crash was found in old OSS-Fuzz build.')
    return False

  def get_test_case(self, error_string):
    """Gets the file from a fuzzer run stack trace.

    Args:
      error_string: The stack trace string containing the error.

    Returns:
      The error test case or None if not found.
    """
    match = re.search(r'\bTest unit written to \.\/([^\s]+)', error_string)
    if match:
      return os.path.join(self.out_dir, match.group(1))
    return None

  def get_lastest_build_version(self):
    """Gets the latest OSS-Fuzz build version for a projects fuzzers.

    Returns:
      A string with the latest build version or None.
    """
    if not self.project_name:
      return None
    sanitizer = 'address'
    version = '{project_name}-{sanitizer}-latest.version'.format(
        project_name=self.project_name, sanitizer=sanitizer)
    version_url = url_join(GCS_BASE_URL, self.project_name, version)
    try:
      response = urllib.request.urlopen(version_url)
    except urllib.error.HTTPError:
      logging.error('Error getting the lastest build version for %s.',
                    self.project_name)
      return None
    return response.read().decode('UTF-8')

  def download_oss_fuzz_build(self):
    """Downloads the latest OSS-Fuzz build from GCS.

    Returns:
      A path to where the OSS-Fuzz build is located, or None.
    """
    if not os.path.exists(self.out_dir):
      logging.error('Out directory %s does not exist.', self.out_dir)
      return None
    if not self.project_name:
      return None
    build_dir = os.path.join(self.out_dir, 'oss_fuzz_latest', self.project_name)
    if os.path.exists(os.path.join(build_dir, self.target_name)):
      return build_dir
    os.makedirs(build_dir, exist_ok=True)
    latest_build_str = self.get_lastest_build_version()
    if not latest_build_str:
      return None

    oss_fuzz_build_url = url_join(GCS_BASE_URL, self.project_name,
                                  latest_build_str)
    try:
      urllib.request.urlretrieve(oss_fuzz_build_url, BUILD_STORE_NAME)
    except urllib.error.HTTPError:
      logging.error('Unable to download build from: %s.', oss_fuzz_build_url)
      return None
    with zipfile.ZipFile(BUILD_STORE_NAME, 'r') as zip_file:
      zip_file.extractall(build_dir)
    return build_dir


def url_join(*argv):
  """Joins URLs together using the posix join method.

  Args:
    argv: Sections of a URL to be joined.

  Returns:
    Joined URL.
  """
  return posixpath.join(*argv)
