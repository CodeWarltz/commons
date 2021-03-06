# ==================================================================================================
# Copyright 2012 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

import os
import shutil

from collections import defaultdict

from twitter.common.collections.orderedset import OrderedSet
from twitter.common.dirutil import safe_mkdir

from twitter.pants import (
    binary_util,
    get_buildroot)
from twitter.pants.base.target import Target
from twitter.pants.goal.phase import Phase
from twitter.pants.targets.jvm_binary import JvmBinary
from twitter.pants.tasks.checkstyle import Checkstyle

from .jvm_binary_task import JvmBinaryTask

from . import TaskError


# We use custom checks for scala and java targets here for 2 reasons:
# 1.) jvm_binary could have either a scala or java source file attached so we can't do a pure
#     target type test
# 2.) the target may be under development in which case it may not have sources yet - its pretty
#     common to write a BUILD and ./pants goal idea the target inside to start development at which
#     point there are no source files yet - and the developer intents to add them using the ide.

def is_scala(target):
  return target.has_sources('.scala') or target.is_scala


def is_java(target):
  return target.has_sources('.java') or target.is_java


class IdeGen(JvmBinaryTask):
  @classmethod
  def setup_parser(cls, option_group, args, mkflag):
    option_group.add_option(mkflag("project-name"), dest="ide_gen_project_name", default="project",
                            help="[%default] Specifies the name to use for the generated project.")

    gen_dir = mkflag("project-dir")
    option_group.add_option(gen_dir, dest="ide_gen_project_dir",
                            help="[%default] Specifies the directory to output the generated "
                                "project files to.")
    option_group.add_option(mkflag("project-cwd"), dest="ide_gen_project_cwd",
                            help="[%%default] Specifies the directory the generated project should "
                                 "use as the cwd for processes it launches.  Note that specifying "
                                 "this trumps %s and not all project related files will be stored "
                                 "there." % gen_dir)

    option_group.add_option(mkflag("intransitive"), default=False,
                            action="store_true", dest='ide_gen_intransitive',
                            help="Limits the sources included in the generated project to just "
                                 "those owned by the targets specified on the command line")

    option_group.add_option(mkflag("python"), mkflag("python", negate=True), default=False,
                            action="callback", callback=mkflag.set_bool, dest='ide_gen_python',
                            help="[%default] Adds python support to the generated project "
                                 "configuration.")

    option_group.add_option(mkflag("java"), mkflag("java", negate=True), default=True,
                            action="callback", callback=mkflag.set_bool, dest='ide_gen_java',
                            help="[%default] Includes java sources in the project; otherwise "
                                 "compiles them and adds them to the project classpath.")
    java_language_level = mkflag("java-language-level")
    # TODO(John Sirois): Advance the default to 7 when 8 is released.
    option_group.add_option(java_language_level, default=6,
                            dest="ide_gen_java_language_level", type="int",
                            help="[%default] Sets the java language and jdk used to compile the "
                                 "project's java sources.")
    option_group.add_option(mkflag("java-jdk-name"), default=None,
                            dest="ide_gen_java_jdk",
                            help="Sets the jdk used to compile the project's java sources. If "
                                 "unset the default jdk name for the "
                                 "%s is used." % java_language_level)

    option_group.add_option(mkflag("scala"), mkflag("scala", negate=True), default=True,
                            action="callback", callback=mkflag.set_bool, dest='ide_gen_scala',
                            help="[%default] Includes scala sources in the project; otherwise "
                                 "compiles them and adds them to the project classpath.")

  def __init__(self, context):
    super(IdeGen, self).__init__(context)

    self.project_name = context.options.ide_gen_project_name
    self.python = context.options.ide_gen_python
    self.skip_java = not context.options.ide_gen_java
    self.skip_scala = not context.options.ide_gen_scala

    self.java_language_level = context.options.ide_gen_java_language_level
    if context.options.ide_gen_java_jdk:
      self.java_jdk = context.options.ide_gen_java_jdk
    else:
      self.java_jdk = '1.%d' % self.java_language_level

    self.work_dir = os.path.abspath(
      context.options.ide_gen_project_dir
      or os.path.join(
        context.config.get('ide', 'workdir'), self.__class__.__name__, self.project_name
      )
    )
    self.cwd = (
      os.path.abspath(context.options.ide_gen_project_cwd) if context.options.ide_gen_project_cwd
      else self.work_dir
    )

    self.intransitive = context.options.ide_gen_intransitive

    self.checkstyle_suppression_files = context.config.getdefault(
      'checkstyle_suppression_files', type=list, default=[]
    )
    self.debug_port = context.config.getint('ide', 'debug_port')

    self.checkstyle_bootstrap_key = 'checkstyle'
    checkstyle = context.config.getlist('checkstyle', 'bootstrap-tools',
                                        default=[':twitter-checkstyle'])
    self._jvm_tool_bootstrapper.register_jvm_tool(self.checkstyle_bootstrap_key, checkstyle)

    self.scalac_bootstrap_key = None
    if not self.skip_scala:
      self.scalac_bootstrap_key = 'scalac'
      scalac = context.config.getlist('scala-compile', 'compile-bootstrap-tools',
                                      default=[':scala-compile-2.9.3'])
      self._jvm_tool_bootstrapper.register_jvm_tool(self.scalac_bootstrap_key, scalac)

    targets, self._project = self.configure_project(
        context.targets(),
        self.checkstyle_suppression_files,
        self.debug_port)

    self.configure_compile_context(targets)

    if self.python:
      self.context.products.require('python')
    if not self.skip_java:
      self.context.products.require('java')
    if not self.skip_scala:
      self.context.products.require('scala')

    self.context.products.require('jars')
    self.context.products.require('source_jars')

  def configure_project(self, targets, checkstyle_suppression_files, debug_port):

    jvm_targets = Target.extract_jvm_targets(targets)
    if self.intransitive:
      jvm_targets = set(self.context.target_roots).intersection(jvm_targets)
    project = Project(self.project_name,
                      self.python,
                      self.skip_java,
                      self.skip_scala,
                      get_buildroot(),
                      checkstyle_suppression_files,
                      debug_port,
                      jvm_targets,
                      not self.intransitive,
                      self.context.new_workunit)

    if self.python:
      python_source_paths = self.context.config.getlist('ide', 'python_source_paths', default=[])
      python_test_paths = self.context.config.getlist('ide', 'python_test_paths', default=[])
      python_lib_paths = self.context.config.getlist('ide', 'python_lib_paths', default=[])
      project.configure_python(python_source_paths, python_test_paths, python_lib_paths)

    extra_source_paths = self.context.config.getlist('ide', 'extra_jvm_source_paths', default=[])
    extra_test_paths = self.context.config.getlist('ide', 'extra_jvm_test_paths', default=[])
    all_targets = project.configure_jvm(extra_source_paths, extra_test_paths)
    return all_targets, project

  def configure_compile_context(self, targets):
    """
      Trims the context's target set to just those targets needed as jars on the IDE classpath.
      All other targets only contribute their external jar dependencies and excludes to the
      classpath definition.
    """
    def is_cp(target):
      return (
        target.is_codegen or

        # Some IDEs need annotation processors pre-compiled, others are smart enough to detect and
        # proceed in 2 compile rounds
        target.is_apt or

        (self.skip_java and is_java(target)) or
        (self.skip_scala and is_scala(target)) or
        (self.intransitive and target not in self.context.target_roots)
      )

    jars = OrderedSet()
    excludes = OrderedSet()
    compiles = OrderedSet()
    def prune(target):
      if target.is_jvm:
        if target.excludes:
          excludes.update(target.excludes)
        jars.update(jar for jar in target.jar_dependencies if jar.rev)
        if is_cp(target):
          target.walk(compiles.add)

    for target in targets:
      target.walk(prune)

    self.context.replace_targets(compiles)

    self.binary = self.context.add_new_target(self.work_dir,
                                              JvmBinary,
                                              name='%s-external-jars' % self.project_name,
                                              dependencies=jars,
                                              excludes=excludes,
                                              configurations=('default', 'sources', 'javadoc'))
    self.require_jar_dependencies(predicate=lambda t: t == self.binary)

    self.context.log.debug('pruned to cp:\n\t%s' % '\n\t'.join(
      str(t) for t in self.context.targets())
    )

  def map_internal_jars(self, targets):
    internal_jar_dir = os.path.join(self.work_dir, 'internal-libs')
    safe_mkdir(internal_jar_dir, clean=True)

    internal_source_jar_dir = os.path.join(self.work_dir, 'internal-libsources')
    safe_mkdir(internal_source_jar_dir, clean=True)

    internal_jars = self.context.products.get('jars')
    internal_source_jars = self.context.products.get('source_jars')
    for target in targets:
      mappings = internal_jars.get(target)
      if mappings:
        for base, jars in mappings.items():
          if len(jars) != 1:
            raise TaskError('Unexpected mapping, multiple jars for %s: %s' % (target, jars))

          jar = jars[0]
          cp_jar = os.path.join(internal_jar_dir, jar)
          shutil.copy(os.path.join(base, jar), cp_jar)

          cp_source_jar = None
          mappings = internal_source_jars.get(target)
          if mappings:
            for base, jars in mappings.items():
              if len(jars) != 1:
                raise TaskError(
                  'Unexpected mapping, multiple source jars for %s: %s' % (target, jars)
                )
              jar = jars[0]
              cp_source_jar = os.path.join(internal_source_jar_dir, jar)
              shutil.copy(os.path.join(base, jar), cp_source_jar)

          self._project.internal_jars.add(ClasspathEntry(cp_jar, source_jar=cp_source_jar))

  def map_external_jars(self):
    external_jar_dir = os.path.join(self.work_dir, 'external-libs')
    safe_mkdir(external_jar_dir, clean=True)

    external_source_jar_dir = os.path.join(self.work_dir, 'external-libsources')
    safe_mkdir(external_source_jar_dir, clean=True)

    external_javadoc_jar_dir = os.path.join(self.work_dir, 'external-libjavadoc')
    safe_mkdir(external_javadoc_jar_dir, clean=True)

    confs = ['default', 'sources', 'javadoc']
    for entry in self.list_jar_dependencies(self.binary, confs=confs):
      jar = entry.get('default')
      if jar:
        cp_jar = os.path.join(external_jar_dir, os.path.basename(jar))
        shutil.copy(jar, cp_jar)

        cp_source_jar = None
        source_jar = entry.get('sources')
        if source_jar:
          cp_source_jar = os.path.join(external_source_jar_dir, os.path.basename(source_jar))
          shutil.copy(source_jar, cp_source_jar)

        cp_javadoc_jar = None
        javadoc_jar = entry.get('javadoc')
        if javadoc_jar:
          cp_javadoc_jar = os.path.join(external_javadoc_jar_dir, os.path.basename(javadoc_jar))
          shutil.copy(javadoc_jar, cp_javadoc_jar)

        self._project.external_jars.add(ClasspathEntry(cp_jar,
                                                       source_jar=cp_source_jar,
                                                       javadoc_jar=cp_javadoc_jar))

  def execute(self, targets):
    """Stages IDE project artifacts to a project directory and generates IDE configuration files."""
    checkstyle_enabled = len(Phase.goals_of_type(Checkstyle)) > 0
    if checkstyle_enabled:
      checkstyle_classpath = self._jvm_tool_bootstrapper.get_jvm_tool_classpath(
          self.checkstyle_bootstrap_key)
    else:
      checkstyle_classpath = []

    if self.scalac_bootstrap_key:
      scalac_classpath = self._jvm_tool_bootstrapper.get_jvm_tool_classpath(
          self.scalac_bootstrap_key)
    else:
      scalac_classpath = []

    self._project.set_tool_classpaths(checkstyle_classpath, scalac_classpath)

    self.map_internal_jars(targets)
    self.map_external_jars()

    idefile = self.generate_project(self._project)
    if idefile:
      binary_util.ui_open(idefile)

  def generate_project(self, project):
    raise NotImplementedError('Subclasses must generate a project for an ide')


class ClasspathEntry(object):
  """Represents a classpath entry that may have sources available."""
  def __init__(self, jar, source_jar=None, javadoc_jar=None):
    self.jar = jar
    self.source_jar = source_jar
    self.javadoc_jar = javadoc_jar


class SourceSet(object):
  """Models a set of source files."""

  TEST_BASES = set()

  def __init__(self, root_dir, source_base, path, is_test):
    """
      root_dir: the full path to the root directory of the project containing this source set
      source_base: the relative path from root_dir to the base of this source set
      path: the relative path from the source_base to the base of the sources in this set
      is_test: true iff the sources contained by this set implement test cases
    """

    self.root_dir = root_dir
    self.source_base = source_base
    self.path = path
    self.is_test = is_test
    self._excludes = []
    if is_test:
      SourceSet.TEST_BASES.add(self.source_base)

  @property
  def excludes(self):
    """Paths relative to self.path that are excluded from this source set."""

    return self._excludes


class Project(object):
  """Models a generic IDE project that is comprised of a set of BUILD targets."""

  @staticmethod
  def extract_resource_extensions(resources):
    """Returns the set of unique extensions (including the .) from the given resource files."""

    if resources:
      for resource in resources:
        _, ext = os.path.splitext(resource)
        yield ext

  def __init__(self, name, has_python, skip_java, skip_scala, root_dir,
               checkstyle_suppression_files, debug_port, targets, transitive, workunit_factory):
    """Creates a new, unconfigured, Project based at root_dir and comprised of the sources visible
    to the given targets."""

    self.name = name
    self.root_dir = root_dir
    self.targets = OrderedSet(targets)
    self.transitive = transitive
    self.workunit_factory = workunit_factory

    self.sources = []
    self.py_sources = []
    self.py_libs = []
    self.resource_extensions = set()

    self.has_python = has_python
    self.skip_java = skip_java
    self.skip_scala = skip_scala
    self.has_scala = False
    self.has_tests = False

    self.checkstyle_suppression_files = checkstyle_suppression_files # Absolute paths.
    self.debug_port = debug_port

    self.internal_jars = OrderedSet()
    self.external_jars = OrderedSet()

  def configure_python(self, source_roots, test_roots, lib_roots):
    self.py_sources.extend(SourceSet(get_buildroot(), root, None, False) for root in source_roots)
    self.py_sources.extend(SourceSet(get_buildroot(), root, None, True) for root in test_roots)
    for root in lib_roots:
      for path in os.listdir(os.path.join(get_buildroot(), root)):
        if os.path.isdir(os.path.join(get_buildroot(), root, path)) or path.endswith('.egg'):
          self.py_libs.append(SourceSet(get_buildroot(), root, path, False))

  def configure_jvm(self, extra_source_paths, extra_test_paths):
    """
      Configures this project's source sets returning the full set of targets the project is
      comprised of.  The full set can be larger than the initial set of targets when any of the
      initial targets only has partial ownership of its source set's directories.
    """

    # TODO(John Sirois): much waste lies here, revisit structuring for more readable and efficient
    # construction of source sets and excludes ... and add a test!

    analyzed = OrderedSet()
    targeted = set()

    def source_target(target):
      return ((self.transitive or target in self.targets) and
              target.has_sources() and
              (not target.is_codegen and
               not (self.skip_java and is_java(target)) and
               not (self.skip_scala and is_scala(target))))

    def configure_source_sets(relative_base, sources, is_test):
      absolute_base = os.path.join(self.root_dir, relative_base)
      paths = set([ os.path.dirname(source) for source in sources])
      for path in paths:
        absolute_path = os.path.join(absolute_base, path)
        if absolute_path not in targeted:
          targeted.add(absolute_path)
          self.sources.append(SourceSet(self.root_dir, relative_base, path, is_test))

    def find_source_basedirs(target):
      dirs = set()
      if source_target(target):
        absolute_base = os.path.join(self.root_dir, target.target_base)
        dirs.update([ os.path.join(absolute_base, os.path.dirname(source))
                      for source in target.sources ])
      return dirs

    def configure_target(target):
      if target not in analyzed:
        analyzed.add(target)

        self.has_scala = not self.skip_scala and (self.has_scala or is_scala(target))

        if target.has_resources:
          resources_by_basedir = defaultdict(set)
          for resources in target.resources:
            resources_by_basedir[resources.target_base].update(resources.sources)
          for basedir, resources in resources_by_basedir.items():
            self.resource_extensions.update(Project.extract_resource_extensions(resources))
            configure_source_sets(basedir, resources, is_test=False)

        if target.sources:
          test = target.is_test
          self.has_tests = self.has_tests or test
          configure_source_sets(target.target_base, target.sources, is_test = test)

        # Other BUILD files may specify sources in the same directory as this target.  Those BUILD
        # files might be in parent directories (globs('a/b/*.java')) or even children directories if
        # this target globs children as well.  Gather all these candidate BUILD files to test for
        # sources they own that live in the directories this targets sources live in.
        target_dirset = find_source_basedirs(target)
        candidates = Target.get_all_addresses(target.address.buildfile)
        for ancestor in target.address.buildfile.ancestors():
          candidates.update(Target.get_all_addresses(ancestor))
        for sibling in target.address.buildfile.siblings():
          candidates.update(Target.get_all_addresses(sibling))
        for descendant in target.address.buildfile.descendants():
          candidates.update(Target.get_all_addresses(descendant))

        def is_sibling(target):
          return source_target(target) and target_dirset.intersection(find_source_basedirs(target))

        return filter(is_sibling, [ Target.get(a) for a in candidates if a != target.address ])

    for target in self.targets:
      target.walk(configure_target, predicate = source_target)

    # We need to figure out excludes, in doing so there are 2 cases we should not exclude:
    # 1.) targets depend on A only should lead to an exclude of B
    # A/BUILD
    # A/B/BUILD
    #
    # 2.) targets depend on A and C should not lead to an exclude of B (would wipe out C)
    # A/BUILD
    # A/B
    # A/B/C/BUILD
    #
    # 1 approach: build set of all paths and parent paths containing BUILDs our targets depend on -
    # these are unexcludable

    unexcludable_paths = set()
    for source_set in self.sources:
      parent = os.path.join(self.root_dir, source_set.source_base, source_set.path)
      while True:
        unexcludable_paths.add(parent)
        parent, _ = os.path.split(parent)
        # no need to add the repo root or above, all source paths and extra paths are children
        if parent == self.root_dir:
          break

    for source_set in self.sources:
      paths = set()
      source_base = os.path.join(self.root_dir, source_set.source_base)
      for root, dirs, _ in os.walk(os.path.join(source_base, source_set.path)):
        if dirs:
          paths.update([os.path.join(root, directory) for directory in dirs])
      unused_children = paths - targeted
      if unused_children:
        for child in unused_children:
          if child not in unexcludable_paths:
            source_set.excludes.append(os.path.relpath(child, source_base))

    targets = OrderedSet()
    for target in self.targets:
      target.walk(lambda target: targets.add(target), source_target)
    targets.update(analyzed - targets)

    self.sources.extend(SourceSet(get_buildroot(), p, None, False) for p in extra_source_paths)
    self.sources.extend(SourceSet(get_buildroot(), p, None, True) for p in extra_test_paths)

    return targets

  def set_tool_classpaths(self, checkstyle_classpath, scalac_classpath):
    self.checkstyle_classpath = checkstyle_classpath
    self.scala_compiler_classpath = scalac_classpath
