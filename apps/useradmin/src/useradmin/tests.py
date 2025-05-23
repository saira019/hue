#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import sys
import json
import time
import logging
import urllib.parse
from builtins import object
from datetime import datetime
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.db.models import Q
from django.test import override_settings
from django.test.client import Client
from django.urls import reverse

import desktop.conf
import libsaml.conf
import useradmin.conf
import useradmin.ldap_access
from desktop import appmanager
from desktop.auth.backend import create_user, is_admin
from desktop.conf import APP_BLACKLIST, ENABLE_ORGANIZATIONS, ENABLE_PROMETHEUS
from desktop.lib.django_test_util import make_logged_in_client
from desktop.lib.i18n import smart_str
from desktop.lib.test_utils import grant_access
from desktop.views import home
from hadoop import pseudo_hdfs4
from hadoop.pseudo_hdfs4 import is_live_cluster
from useradmin.forms import UserChangeForm
from useradmin.hue_password_policy import reset_password_policy
from useradmin.metrics import active_users, active_users_per_instance
from useradmin.middleware import ConcurrentUserSessionMiddleware
from useradmin.models import Group, GroupPermission, HuePermission, User, UserProfile, get_default_user_group, get_profile

LOG = logging.getLogger()

try:
  from ldap import SCOPE_SUBTREE
except ImportError:
  LOG.warning('ldap module is not available')
  SCOPE_SUBTREE = None


class MockRequest(dict):
  pass


class MockUser(dict):
  def is_authenticated(self):
    return True


class MockSession(dict):
  pass


def reset_all_users():
  """Reset to a clean state by deleting all users"""
  for user in User.objects.all():
    user.delete()


def reset_all_groups():
  """Reset to a clean state by deleting all groups"""
  useradmin.conf.DEFAULT_USER_GROUP.set_for_testing(None)
  for grp in Group.objects.all():
    grp.delete()


def reset_all_user_profile():
  """Reset to a clean state by deleting all user profiles"""
  for up in UserProfile.objects.all():
    up.delete()


class LdapTestConnection(object):
  """
  Test class which mimics the behaviour of LdapConnection (from ldap_access.py).
  It also includes functionality to fake modifications to an LDAP server.  It is designed
  as a singleton, to allow for changes to persist across discrete connections.

  This class assumes uid is the user_name_attr.
  """
  def __init__(self):
    self._instance = LdapTestConnection.Data()

  def add_user_group_for_test(self, user, group):
    self._instance.groups[group]['members'].append(user)

  def remove_user_group_for_test(self, user, group):
    self._instance.groups[group]['members'].remove(user)

  def add_posix_user_group_for_test(self, user, group):
    self._instance.groups[group]['posix_members'].append(user)

  def remove_posix_user_group_for_test(self, user, group):
    self._instance.groups[group]['posix_members'].remove(user)

  def find_users(self, username_pattern, search_attr=None, user_name_attr=None, find_by_dn=False, scope=SCOPE_SUBTREE):
    """ Returns info for a particular user via a case insensitive search """
    if find_by_dn:
      data = [attrs for attrs in list(self._instance.users.values()) if attrs['dn'] == username_pattern]
    else:
      username_pattern = "^%s$" % username_pattern.replace('.', '\\.').replace('*', '.*')
      username_fsm = re.compile(username_pattern, flags=re.I)
      usernames = [username for username in list(self._instance.users.keys()) if username_fsm.match(username)]
      data = [self._instance.users.get(username) for username in usernames]
    return data

  def find_groups(self, groupname_pattern, search_attr=None, group_name_attr=None,
                  group_member_attr=None, group_filter=None, find_by_dn=False, scope=SCOPE_SUBTREE):
    """ Return all groups in the system with parents and children """
    if find_by_dn:
      data = [attrs for attrs in list(self._instance.groups.values()) if attrs['dn'] == groupname_pattern]
      # SCOPE_SUBTREE means we return all sub-entries of the desired entry along with the desired entry.
      if data and scope == SCOPE_SUBTREE:
        sub_data = [attrs for attrs in list(self._instance.groups.values()) if attrs['dn'].endswith(data[0]['dn'])]
        data.extend(sub_data)
    else:
      groupname_pattern = "^%s$" % groupname_pattern.replace('.', '\\.').replace('*', '.*')
      groupnames = [username for username in list(self._instance.groups.keys()) if re.match(groupname_pattern, username)]
      data = [self._instance.groups.get(groupname) for groupname in groupnames]
    return data

  def find_members_of_group(self, dn, search_attr, ldap_filter, scope=SCOPE_SUBTREE):
    members = []
    for group_info in self._instance.groups:
      if group_info['dn'] == dn:
        members.extend(group_info['members'])

    members = set(members)
    users = []
    for user_info in self._instance.users:
      if user_info['dn'] in members:
        users.append(user_info)

    groups = []
    for group_info in self._instance.groups:
      if group_info['dn'] in members:
        groups.append(group_info)

    return users + groups

  def find_users_of_group(self, dn):
    members = []
    for group_info in list(self._instance.groups.values()):
      if group_info['dn'] == dn:
        members.extend(group_info['members'])

    members = set(members)
    users = []
    for user_info in list(self._instance.users.values()):
      if user_info['dn'] in members:
        users.append(user_info)

    return users

  def find_groups_of_group(self, dn):
    members = []
    for group_info in list(self._instance.groups.values()):
      if group_info['dn'] == dn:
        members.extend(group_info['members'])

    groups = []
    for group_info in list(self._instance.groups.values()):
      if group_info['dn'] in members:
        groups.append(group_info)

    return groups

  class Data(object):
    def __init__(self):
      long_username = create_long_username()
      self.users = {
        'moe': {
          'dn': 'uid=moe,ou=People,dc=example,dc=com', 'username': 'moe', 'first': 'Moe', 'email': 'moe@stooges.com',
          'groups': ['cn=TestUsers,ou=Groups,dc=example,dc=com']
        },
        'lårry': {
          'dn': 'uid=lårry,ou=People,dc=example,dc=com', 'username': 'lårry', 'first': 'Larry', 'last': 'Stooge',
          'email': 'larry@stooges.com',
          'groups': ['cn=TestUsers,ou=Groups,dc=example,dc=com', 'cn=Test Administrators,cn=TestUsers,ou=Groups,dc=example,dc=com']
        },
        'curly': {
          'dn': 'uid=curly,ou=People,dc=example,dc=com', 'username': 'curly', 'first': 'Curly', 'last': 'Stooge',
          'email': 'curly@stooges.com',
          'groups': ['cn=TestUsers,ou=Groups,dc=example,dc=com', 'cn=Test Administrators,cn=TestUsers,ou=Groups,dc=example,dc=com']
        },
        'Rock': {
          'dn': 'uid=Rock,ou=People,dc=example,dc=com', 'username': 'Rock', 'first': 'rock', 'last': 'man', 'email': 'rockman@stooges.com',
          'groups': ['cn=Test Administrators,cn=TestUsers,ou=Groups,dc=example,dc=com']
        },
        'nestedguy': {
          'dn': 'uid=nestedguy,ou=People,dc=example,dc=com', 'username': 'nestedguy', 'first': 'nested', 'last': 'guy',
          'email': 'nestedguy@stooges.com', 'groups': ['cn=NestedGroup,ou=Groups,dc=example,dc=com']
        },
        'otherguy': {
          'dn': 'uid=otherguy,ou=People,dc=example,dc=com', 'username': 'otherguy', 'first': 'Other', 'last': 'Guy',
          'email': 'other@guy.com'
        },
        'posix_person': {
          'dn': 'uid=posix_person,ou=People,dc=example,dc=com', 'username': 'posix_person', 'first': 'pos', 'last': 'ix',
          'email': 'pos@ix.com'
        },
        'posix_person2': {
          'dn': 'uid=posix_person2,ou=People,dc=example,dc=com', 'username': 'posix_person2', 'first': 'pos', 'last': 'ix',
          'email': 'pos@ix.com'
        },
        'user with space': {
          'dn': 'uid=user with space,ou=People,dc=example,dc=com', 'username': 'user with space', 'first': 'user', 'last': 'space',
          'email': 'user@space.com'
        },
        'spaceless': {
          'dn': 'uid=user without space,ou=People,dc=example,dc=com', 'username': 'spaceless', 'first': 'user', 'last': 'space',
          'email': 'user@space.com'
        },
        long_username: {
          'dn': 'uid=' + long_username + ',ou=People,dc=example,dc=com', 'username': long_username, 'first': 'toolong', 'last': 'username',
          'email': 'toolong@username.com'
        },
        'test_longfirstname': {
          'dn': 'uid=test_longfirstname,ou=People,dc=example,dc=com', 'username': 'test_longfirstname',
          'first': 'test_longfirstname_test_longfirstname', 'last': 'username', 'email': 'toolong@username.com'
        },
      }

      self.groups = {
        'TestUsers': {
          'dn': 'cn=TestUsers,ou=Groups,dc=example,dc=com',
          'name': 'TestUsers',
          'members': [
            'uid=moe,ou=People,dc=example,dc=com', 'uid=lårry,ou=People,dc=example,dc=com',
            'uid=curly,ou=People,dc=example,dc=com', 'uid=' + long_username + ',ou=People,dc=example,dc=com'
          ],
          'posix_members': []},
        'Test Administrators': {
          'dn': 'cn=Test Administrators,cn=TestUsers,ou=Groups,dc=example,dc=com',
          'name': 'Test Administrators',
          'members': [
            'uid=Rock,ou=People,dc=example,dc=com', 'uid=lårry,ou=People,dc=example,dc=com',
            'uid=curly,ou=People,dc=example,dc=com', 'uid=' + long_username + ',ou=People,dc=example,dc=com'
          ],
          'posix_members': []},
        'OtherGroup': {
          'dn': 'cn=OtherGroup,cn=TestUsers,ou=Groups,dc=example,dc=com',
          'name': 'OtherGroup',
          'members': [],
          'posix_members': []},
        'NestedGroups': {
          'dn': 'cn=NestedGroups,ou=Groups,dc=example,dc=com',
          'name': 'NestedGroups',
          'members': ['cn=NestedGroup,ou=Groups,dc=example,dc=com'],
          'posix_members': []
        },
        'NestedGroup': {
          'dn': 'cn=NestedGroup,ou=Groups,dc=example,dc=com',
          'name': 'NestedGroup',
          'members': ['uid=nestedguy,ou=People,dc=example,dc=com'],
          'posix_members': []
        },
        'NestedPosixGroups': {
          'dn': 'cn=NestedPosixGroups,ou=Groups,dc=example,dc=com',
          'name': 'NestedPosixGroups',
          'members': ['cn=PosixGroup,ou=Groups,dc=example,dc=com'],
          'posix_members': []
        },
        'PosixGroup': {
          'dn': 'cn=PosixGroup,ou=Groups,dc=example,dc=com',
          'name': 'PosixGroup',
          'members': [],
          'posix_members': ['posix_person', 'lårry']},
        'PosixGroup1': {
          'dn': 'cn=PosixGroup1,cn=PosixGroup,ou=Groups,dc=example,dc=com',
          'name': 'PosixGroup1',
          'members': [],
          'posix_members': ['posix_person2']},
        }


def create_long_username():
  return "A" * 151


@pytest.mark.django_db
def test_invalid_username():
  BAD_NAMES = ('-foo', 'foo:o', 'foo o', ' foo')

  c = make_logged_in_client(username="test", is_superuser=True)

  for bad_name in BAD_NAMES:
    assert c.get('/useradmin/users/new')
    response = c.post('/useradmin/users/new', dict(username=bad_name, password1="test", password2="test"))
    assert 'not allowed' in response.context[0]["form"].errors['username'][0]


class BaseUserAdminTests(object):

  @classmethod
  def setup_class(cls):
    cls._class_resets = [
      useradmin.conf.DEFAULT_USER_GROUP.set_for_testing(None),
    ]

  @classmethod
  def teardown_class(cls):
    for reset in cls._class_resets:
      reset()

  def setup_method(self):
    reset_all_users()
    reset_all_groups()

  def teardown_method(self):
    pass


@pytest.mark.django_db
class TestUserProfile(BaseUserAdminTests):

  @override_settings(AUTHENTICATION_BACKENDS=['desktop.auth.backend.AllowFirstUserDjangoBackend'])
  def test_get_profile(self):
    '''Ensure profiles are created after get_profile is called.'''
    user = create_user(username='test', password='test', is_superuser=False)

    assert 0 == UserProfile.objects.filter(user=user).count()

    p = get_profile(user)

    assert 1 == UserProfile.objects.filter(user=user).count()

  @override_settings(AUTHENTICATION_BACKENDS=['desktop.auth.backend.AllowFirstUserDjangoBackend'])
  def test_get_and_update_profile(self):
    c = make_logged_in_client(username='test', password='test', is_superuser=False, recreate=True)

    user = User.objects.get(username='test')
    userprofile = get_profile(user)
    assert not userprofile.data.get('language_preference')

    userprofile.update_data({'language_preference': 'en'})
    userprofile.save()
    assert 'en' == userprofile.data['language_preference']

    userprofile.update_data({'language_preference': 'es'})
    userprofile.save()
    assert 'es' == userprofile.data['language_preference']

    user = User.objects.get(username='test')
    userprofile = get_profile(user)
    assert 'es' == userprofile.data['language_preference']


@pytest.mark.django_db
class TestSAMLGroupsCheck(BaseUserAdminTests):
  def test_saml_group_conditions_check(self):
    if sys.version_info[0] > 2:
      pytest.skip("Skipping Test")
    reset = []
    old_settings = settings.AUTHENTICATION_BACKENDS
    try:
      c = make_logged_in_client(username='test2', password='test2', is_superuser=False, recreate=True)
      settings.AUTHENTICATION_BACKENDS = ["libsaml.backend.SAML2Backend"]
      request = MockRequest()

      user = User.objects.get(username='test2')
      userprofile = get_profile(user)
      request.user = user

      # In case of no valid saml response from server.
      reset.append(libsaml.conf.REQUIRED_GROUPS_ATTRIBUTE.set_for_testing("groups"))
      reset.append(libsaml.conf.REQUIRED_GROUPS.set_for_testing(["ddd"]))
      assert not desktop.views.samlgroup_check(request)

      # mock saml response
      userprofile.update_data({"saml_attributes": {"first_name": ["test2"],
                                                  "last_name": ["test2"],
                                                  "email": ["test2@test.com"],
                                                  "groups": ["aaa", "bbb", "ccc"]}})
      userprofile.save()

      # valid one or more valid required groups
      reset.append(libsaml.conf.REQUIRED_GROUPS_ATTRIBUTE.set_for_testing("groups"))
      reset.append(libsaml.conf.REQUIRED_GROUPS.set_for_testing(["aaa", "ddd"]))
      assert desktop.views.samlgroup_check(request)

      # invalid required group
      reset.append(libsaml.conf.REQUIRED_GROUPS_ATTRIBUTE.set_for_testing("groups"))
      reset.append(libsaml.conf.REQUIRED_GROUPS.set_for_testing(["ddd"]))
      assert not desktop.views.samlgroup_check(request)

      # different samlresponse for group attribute
      reset.append(libsaml.conf.REQUIRED_GROUPS_ATTRIBUTE.set_for_testing("members"))
      reset.append(libsaml.conf.REQUIRED_GROUPS.set_for_testing(["ddd"]))
      assert not desktop.views.samlgroup_check(request)
    finally:
      settings.AUTHENTICATION_BACKENDS = old_settings
      for r in reset:
        r()


@pytest.mark.django_db
class TestUserAdminMetrics(BaseUserAdminTests):

  def setup_method(self):
    super(TestUserAdminMetrics, self).setup_method()
    reset_all_user_profile()

    with patch('useradmin.middleware.get_localhost_name') as get_hostname:
      get_hostname.return_value = 'host1'

      c = make_logged_in_client(username='test1', password='test', is_superuser=False, recreate=True)
      userprofile1 = get_profile(User.objects.get(username='test1'))
      userprofile1.last_activity = datetime.now()
      userprofile1.first_login = False
      userprofile1.hostname = 'host1'
      userprofile1.save()

      c = make_logged_in_client(username='test2', password='test', is_superuser=False, recreate=True)
      userprofile2 = get_profile(User.objects.get(username='test2'))
      userprofile2.last_activity = datetime.now()
      userprofile2.first_login = False
      userprofile2.hostname = 'host1'
      userprofile2.save()

      User.objects.create_user(username='new_user', password='password')
      new_userprofile = get_profile(User.objects.get(username='new_user'))
      new_userprofile.last_activity = datetime.now()
      new_userprofile.first_login = True
      new_userprofile.save()

    with patch('useradmin.middleware.get_localhost_name') as get_hostname:
      get_hostname.return_value = 'host2'

      c = make_logged_in_client(username='test3', password='test', is_superuser=False, recreate=True)
      userprofile3 = get_profile(User.objects.get(username='test3'))
      userprofile3.last_activity = datetime.now()
      userprofile3.first_login = False
      userprofile3.hostname = 'host2'
      userprofile3.save()

  def teardown_method(self):
    reset_all_user_profile()
    super(TestUserAdminMetrics, self).teardown_method()

  @override_settings(AUTHENTICATION_BACKENDS=['desktop.auth.backend.AllowFirstUserDjangoBackend'])
  def test_active_users(self):
    with patch('useradmin.metrics.get_localhost_name') as get_hostname:
      get_hostname.return_value = 'host1'
      assert 3 == active_users()
      assert 2 == active_users_per_instance()

      c = Client()
      response = c.get('/desktop/metrics/', {'format': 'json'})

      metric = json.loads(response.content)['metric']
      assert 3 == metric['users.active.total']['value']
      assert 2 == metric['users.active']['value']

  @override_settings(AUTHENTICATION_BACKENDS=['desktop.auth.backend.AllowFirstUserDjangoBackend'])
  def test_active_users_prometheus(self):
    if not ENABLE_PROMETHEUS.get():
      pytest.skip("Skipping Test")

    with patch('useradmin.metrics.get_localhost_name') as get_hostname:
      get_hostname.return_value = 'host1'
      c = Client()
      response = c.get('/metrics')
      assert b'hue_active_users 3.0' in response.content, response.content
      assert b'hue_local_active_users 2.0' in response.content, response.content


@pytest.mark.django_db
class TestUserAdmin(BaseUserAdminTests):

  def test_group_permissions(self):
    # Get ourselves set up with a user and a group
    c = make_logged_in_client(username="test", is_superuser=True)
    Group.objects.create(name="test-group")
    test_user = User.objects.get(username="test")
    test_user.groups.add(Group.objects.get(name="test-group"))
    test_user.save()

    # Make sure that a superuser can always access applications
    response = c.get('/useradmin/users')
    assert b'Users' in response.content

    assert len(GroupPermission.objects.all()) == 0
    c.post('/useradmin/groups/edit/test-group', dict(
        name="test-group",
        members=[User.objects.get(username="test").pk],
        permissions=[HuePermission.objects.get(app='useradmin', action='access').pk],
        save="Save"
      ),
      follow=True
    )
    assert len(GroupPermission.objects.all()) == 1

    # Get ourselves set up with a user and a group with superuser group priv
    cadmin = make_logged_in_client(username="supertest", is_superuser=True)
    Group.objects.create(name="super-test-group")
    cadmin.post('/useradmin/groups/edit/super-test-group', {
        'name': "super-test-group",
        'members': [User.objects.get(username="supertest").pk],
        'permissions': [HuePermission.objects.get(app='useradmin', action='superuser').pk],
        "save": "Save"
      },
      follow=True
    )
    assert len(GroupPermission.objects.all()) == 2

    supertest = User.objects.get(username="supertest")
    supertest.groups.add(Group.objects.get(name="super-test-group"))
    supertest.is_superuser = False
    supertest.save()
    # Validate user is not a checked superuser
    assert not supertest.is_superuser
    # Validate user is superuser by group
    assert UserProfile.objects.get(user__username='supertest').has_hue_permission(action="superuser", app="useradmin") == 1

    # Make sure that a user of supergroup can access /useradmin/users
    # Create user to try to edit
    notused = User.objects.get_or_create(username="notused", is_superuser=False)
    response = cadmin.get('/useradmin/users/edit/notused?is_embeddable=true')
    assert b'User notused' in response.content

    # Make sure we can modify permissions
    response = cadmin.get('/useradmin/permissions/edit/useradmin/access/?is_embeddable=true')
    assert b'Permissions' in response.content
    assert b'Edit useradmin' in response.content, response.content

    # Revoke superuser privilege from groups
    c.post('/useradmin/permissions/edit/useradmin/superuser', dict(
        app='useradmin',
        priv='superuser',
        groups=[],
        save="Save"
      ),
      follow=True
    )
    assert GroupPermission.objects.count() == 1

    # Now test that we have limited access
    c1 = make_logged_in_client(username="nonadmin", is_superuser=False)
    response = c1.get('/useradmin/users')
    assert b'You do not have permission to access the Useradmin application.' in response.content

    # Add the non-admin to a group that should grant permissions to the app
    test_user = User.objects.get(username="nonadmin")
    test_user.groups.add(Group.objects.get(name='test-group'))
    test_user.save()

    # Make sure that a user of nonadmin fails where supertest succeeds
    response = c1.get("/useradmin/users/edit/notused?is_embeddable=true")
    assert b'You must be a superuser to add or edit another user' in response.content

    response = c1.get("/useradmin/permissions/edit/useradmin/access/?is_embeddable=true")
    assert b'You must be a superuser to change permissions' in response.content

    # Check that we have access now
    response = c1.get('/useradmin/users')
    assert get_profile(test_user).has_hue_permission('access', 'useradmin')
    assert b'Users' in response.content

    # Make sure we can't modify permissions
    response = c1.get('/useradmin/permissions/edit/useradmin/access')
    assert b'must be a superuser to change permissions' in response.content

    # And revoke access from the group
    c.post('/useradmin/permissions/edit/useradmin/access', dict(
        app='useradmin',
        priv='access',
        groups=[],
        save="Save"
      ),
      follow=True
    )
    assert len(GroupPermission.objects.all()) == 0
    assert not get_profile(test_user).has_hue_permission('access', 'useradmin')

    # We should no longer have access to the app
    response = c1.get('/useradmin/users')
    assert b'You do not have permission to access the Useradmin application.' in response.content

  def test_list_permissions(self):
    c1 = make_logged_in_client(username="nonadmin", is_superuser=False)
    grant_access('nonadmin', 'nonadmin', 'useradmin')
    grant_access('nonadmin', 'nonadmin', 'beeswax')

    response = c1.get('/useradmin/permissions/')
    assert 200 == response.status_code

    perms = response.context[0]['permissions']
    assert perms.filter(app='beeswax').exists(), perms  # Assumes beeswax is there

    reset = APP_BLACKLIST.set_for_testing('beeswax')
    appmanager.DESKTOP_MODULES = []
    appmanager.DESKTOP_APPS = None
    appmanager.load_apps(APP_BLACKLIST.get())
    try:
      response = c1.get('/useradmin/permissions/')
      perms = response.context[0]['permissions']
      assert not perms.filter(app='beeswax').exists(), perms  # beeswax is not there now
    finally:
      reset()
      appmanager.DESKTOP_MODULES = []
      appmanager.DESKTOP_APPS = None
      appmanager.load_apps(APP_BLACKLIST.get())

  def test_list_users(self):
    c = make_logged_in_client(username="test", is_superuser=True)

    response = c.get('/useradmin/users')

    assert b'Is admin' in response.content
    assert b'fa fa-check' in response.content

    assert b'Is active' in response.content

  def test_default_group(self):
    resets = [
      useradmin.conf.DEFAULT_USER_GROUP.set_for_testing('test_default')
    ]

    try:
      get_default_user_group()

      c = make_logged_in_client(username='test', is_superuser=True)

      # Create default group if it doesn't already exist.
      assert Group.objects.filter(name='test_default').exists()

      # Try deleting the default group
      assert Group.objects.filter(name='test_default').exists()
      response = c.post('/useradmin/groups/delete', {'group_names': ['test_default']})
      assert b'default user group may not be deleted' in response.content
      assert Group.objects.filter(name='test_default').exists()

      # Change the name of the default group, and try deleting again
      resets.append(useradmin.conf.DEFAULT_USER_GROUP.set_for_testing('new_default'))

      response = c.post('/useradmin/groups/delete', {'group_names': ['test_default']})
      assert not Group.objects.filter(name='test_default').exists()
      assert Group.objects.filter(name='new_default').exists()
    finally:
      for reset in resets:
        reset()

  def test_group_admin(self):
    c = make_logged_in_client(username="test", is_superuser=True)
    response = c.get('/useradmin/groups')
    # No groups just yet
    assert len(response.context[0]["groups"]) == 0
    assert b"Groups" in response.content

    # Create a group
    response = c.get('/useradmin/groups/new')
    assert '/useradmin/groups/new' == response.context[0]['action']
    c.post('/useradmin/groups/new', dict(name="testgroup"))

    # We should have an empty group in the DB now
    assert len(Group.objects.all()) == 1
    assert Group.objects.filter(name="testgroup").exists()
    assert len(Group.objects.get(name="testgroup").user_set.all()) == 0

    # And now, just for kicks, let's try adding a user
    response = c.post('/useradmin/groups/edit/testgroup',
                      dict(name="testgroup",
                      members=[User.objects.get(username="test").pk],
                      save="Save"), follow=True)
    assert len(Group.objects.get(name="testgroup").user_set.all()) == 1
    assert Group.objects.get(name="testgroup").user_set.filter(username="test").exists()

    # Test some permissions
    c2 = make_logged_in_client(username="nonadmin", is_superuser=False)

    # Need to give access to the user for the rest of the test
    group = Group.objects.create(name="access-group")
    perm = HuePermission.objects.get(app='useradmin', action='access')
    GroupPermission.objects.create(group=group, hue_permission=perm)
    test_user = User.objects.get(username="nonadmin")
    test_user.groups.add(Group.objects.get(name="access-group"))
    test_user.save()

    # Make sure non-superusers can't do bad things
    response = c2.get('/useradmin/groups/new')
    assert b"You must be a superuser" in response.content
    response = c2.get('/useradmin/groups/edit/testgroup')
    assert b"You must be a superuser" in response.content

    response = c2.post('/useradmin/groups/new', dict(name="nonsuperuser"))
    assert b"You must be a superuser" in response.content
    response = c2.post('/useradmin/groups/edit/testgroup',
                      dict(name="nonsuperuser",
                      members=[User.objects.get(username="test").pk],
                      save="Save"), follow=True)
    assert b"You must be a superuser" in response.content

    # Should be one group left, because we created the other group
    response = c.post('/useradmin/groups/delete', {'group_names': ['testgroup']})
    assert len(Group.objects.all()) == 1

    group_count = len(Group.objects.all())
    response = c.post('/useradmin/groups/new', dict(name="with space"))
    assert len(Group.objects.all()) == group_count + 1

  def test_user_admin_password_policy(self):
    # Set up password policy
    password_hint = password_error_msg = ("The password must be at least 8 characters long, "
                                          "and must contain both uppercase and lowercase letters, "
                                          "at least one number, and at least one special character.")
    password_rule = r"^(?=.*?[A-Z])(?=(.*[a-z]){1,})(?=(.*[\d]){1,})(?=(.*[\W_]){1,}).{8,}$"

    resets = [
      useradmin.conf.PASSWORD_POLICY.IS_ENABLED.set_for_testing(True),
      useradmin.conf.PASSWORD_POLICY.PWD_RULE.set_for_testing(password_rule),
      useradmin.conf.PASSWORD_POLICY.PWD_HINT.set_for_testing(password_hint),
      useradmin.conf.PASSWORD_POLICY.PWD_ERROR_MESSAGE.set_for_testing(password_error_msg),
    ]

    try:
      reset_password_policy()

      # Test first-ever login with password policy enabled
      c = Client()

      response = c.get('/hue/accounts/login/')
      assert 200 == response.status_code
      assert response.context[0]['first_login_ever']

      response = c.post('/hue/accounts/login/', dict(username="test_first_login", password="foo"))
      assert response.context[0]['first_login_ever']
      assert [password_error_msg] == response.context[0]["form"]["password"].errors

      response = c.post('/hue/accounts/login/', dict(username="test_first_login", password="foobarTest1["), follow=True)
      assert 200 == response.status_code
      assert User.objects.get(username="test_first_login").is_superuser
      assert User.objects.get(username="test_first_login").check_password("foobarTest1[")

      c.get('/accounts/logout')

      # Test changing a user's password
      c = make_logged_in_client('superuser', is_superuser=True)

      # Test password hint is displayed
      response = c.get('/useradmin/users/edit/superuser')
      assert password_hint in (response.content if isinstance(response.content, str) else response.content.decode())

      # Password is less than 8 characters
      response = c.post('/useradmin/users/edit/superuser',
                        dict(username="superuser",
                             is_superuser=True,
                             password1="foo",
                             password2="foo"))
      assert [password_error_msg] == response.context[0]["form"]["password1"].errors

      # Password is more than 8 characters long but does not have a special character
      response = c.post('/useradmin/users/edit/superuser',
                        dict(username="superuser",
                             is_superuser=True,
                             password1="foobarTest1",
                             password2="foobarTest1"))
      assert [password_error_msg] == response.context[0]["form"]["password1"].errors

      # Password1 and Password2 are valid but they do not match
      response = c.post('/useradmin/users/edit/superuser',
                        dict(username="superuser",
                             is_superuser=True,
                             password1="foobarTest1??",
                             password2="foobarTest1?",
                             password_old="foobarTest1[",
                             is_active=True))
      assert ["Passwords do not match."] == response.context[0]["form"]["password2"].errors

      # Password is valid now
      c.post('/useradmin/users/edit/superuser',
             dict(username="superuser",
                  is_superuser=True,
                  password1="foobarTest1[",
                  password2="foobarTest1[",
                  password_old="test",
                  is_active=True))
      assert User.objects.get(username="superuser").is_superuser
      assert User.objects.get(username="superuser").check_password("foobarTest1[")

      # Test creating a new user
      response = c.get('/useradmin/users/new')
      c = make_logged_in_client('superuser', 'foobarTest1[', is_superuser=True)

      # Password is more than 8 characters long but does not have a special character
      response = c.post('/useradmin/users/new',
                        dict(username="test_user",
                             is_superuser=False,
                             password1="foo",
                             password2="foo"))
      assert ({'password1': [password_error_msg], 'password2': [password_error_msg]} ==
                   response.context[0]["form"].errors)

      # Password is more than 8 characters long but does not have a special character
      response = c.post('/useradmin/users/new',
                        dict(username="test_user",
                             is_superuser=False,
                             password1="foobarTest1",
                             password2="foobarTest1"))

      assert ({'password1': [password_error_msg], 'password2': [password_error_msg]} ==
                   response.context[0]["form"].errors)

      # Password1 and Password2 are valid but they do not match
      response = c.post('/useradmin/users/new',
                        dict(username="test_user",
                             is_superuser=False,
                             password1="foobarTest1[",
                             password2="foobarTest1?"))
      assert {'password2': ["Passwords do not match."]} == response.context[0]["form"].errors

      # Password is valid now
      c.post('/useradmin/users/new',
             dict(username="test_user",
                  is_superuser=False,
                  password1="foobarTest1[",
                  password2="foobarTest1[", is_active=True))
      assert not User.objects.get(username="test_user").is_superuser
      assert User.objects.get(username="test_user").check_password("foobarTest1[")
    finally:
      for reset in resets:
        reset()

  def test_user_admin(self):
    FUNNY_NAME = 'أحمد@cloudera.com'
    FUNNY_NAME_QUOTED = urllib.parse.quote(FUNNY_NAME)

    resets = [
      useradmin.conf.DEFAULT_USER_GROUP.set_for_testing('test_default'),
      useradmin.conf.PASSWORD_POLICY.IS_ENABLED.set_for_testing(False),
    ]

    try:
      reset_password_policy()

      c = make_logged_in_client('test', is_superuser=True)
      user = User.objects.get(username='test')

      # Test basic output.
      response = c.get('/useradmin/')
      assert len(response.context[0]["users"]) > 0
      assert b"Users" in response.content

      # Test editing a superuser
      # Just check that this comes back
      response = c.get('/useradmin/users/edit/test')
      # Edit it, to add a first and last name
      response = c.post('/useradmin/users/edit/test', dict(
          username="test",
          first_name=u"Inglés",
          last_name=u"Español",
          is_superuser=True,
          is_active=True
        ),
        follow=True
      )
      assert b"User information updated" in response.content, "Notification should be displayed in: %s" % response.content
      # Edit it, can't change username
      response = c.post('/useradmin/users/edit/test', dict(
          username="test2",
          first_name=u"Inglés",
          last_name=u"Español",
          is_superuser=True,
          is_active=True
        ),
        follow=True
      )
      assert b"You cannot change a username" in response.content
      # Now make sure that those were materialized
      response = c.get('/useradmin/users/edit/test')
      assert smart_str("Inglés") == response.context[0]["form"].instance.first_name
      assert ("Español" if isinstance(response.content, str) else "Español".encode('utf-8')) in response.content
      # Shouldn't be able to demote to non-superuser
      response = c.post('/useradmin/users/edit/test', dict(
          username="test",
          first_name=u"Inglés",
          last_name=u"Español",
          is_superuser=False,
          is_active=True
        )
      )
      assert b"You cannot remove" in response.content, "Shouldn't be able to remove the last superuser"
      # Shouldn't be able to delete oneself
      response = c.post('/useradmin/users/delete', {u'user_ids': [user.id], 'is_delete': True})
      assert b"You cannot remove yourself" in response.content, "Shouldn't be able to delete the last superuser"

      # Let's try changing the password
      response = c.post('/useradmin/users/edit/test', dict(
          username="test",
          first_name="Tom",
          last_name="Tester",
          is_superuser=True,
          password1="foo",
          password2="foobar"
        )
      )
      assert (
        ["Passwords do not match."] == response.context[0]["form"]["password2"].errors), "Should have complained about mismatched password"
      # Old password not confirmed
      response = c.post('/useradmin/users/edit/test', dict(
          username="test",
          first_name="Tom",
          last_name="Tester",
          password1="foo",
          password2="foo",
          is_active=True,
          is_superuser=True
        )
      )
      assert (
        [UserChangeForm.GENERIC_VALIDATION_ERROR] ==
        response.context[0]["form"]["password_old"].errors), "Should have complained about old password"
      # Good now
      response = c.post('/useradmin/users/edit/test', dict(
          username="test",
          first_name="Tom",
          last_name="Tester",
          password1="foo",
          password2="foo",
          password_old="test",
          is_active=True,
          is_superuser=True
        )
      )
      assert User.objects.get(username="test").is_superuser
      assert User.objects.get(username="test").check_password("foo")
      # Change it back!
      response = c.post('/hue/accounts/login/', dict(username="test", password="foo"), follow=True)

      response = c.post(
        '/useradmin/users/edit/test',
        dict(
          username="test", first_name="Tom", last_name="Tester", password1="test",
          password2="test", password_old="foo", is_active=True, is_superuser=True
        )
      )
      response = c.post('/hue/accounts/login/', dict(username="test", password="test"), follow=True)

      assert User.objects.get(username="test").check_password("test")
      assert make_logged_in_client(username="test", password="test"), "Check that we can still login."

      # Check new user form for default group
      group = get_default_user_group()
      response = c.get('/useradmin/users/new')
      assert response
      assert (('<option value="%s" selected>%s</option>' % (group.id, group.name)) in
        (response.content if isinstance(response.content, str) else response.content.decode()))

      # Create a new regular user (duplicate name)
      response = c.post('/useradmin/users/new', dict(username="test", password1="test", password2="test"))
      assert {'username': ['Username already exists.']} == response.context[0]["form"].errors

      # Create a new regular user (for real)
      response = c.post('/useradmin/users/new', dict(
          username=FUNNY_NAME,
          password1="test",
          password2="test",
          is_superuser=True,
          is_active=True
        ),
        follow=True
      )
      if response.status_code != 200:
        assert not response.context[0]["form"].errors
      assert response.status_code == 200, response.content

      response = c.get('/useradmin/')
      assert FUNNY_NAME in (response.content if isinstance(response.content, str) else response.content.decode()), response.content
      assert len(response.context[0]["users"]) > 1
      assert b"Users" in response.content
      # Validate profile is created.
      assert UserProfile.objects.filter(user__username=FUNNY_NAME).exists()

      # Need to give access to the user for the rest of the test
      group = Group.objects.create(name="test-group")
      perm = HuePermission.objects.get(app='useradmin', action='access')
      GroupPermission.objects.create(group=group, hue_permission=perm)

      # Verify that we can modify user groups through the user admin pages
      response = c.post('/useradmin/users/new', dict(username="group_member", password1="test", password2="test", groups=[group.pk]))
      User.objects.get(username='group_member')
      assert User.objects.get(username='group_member').groups.filter(name='test-group').exists()
      response = c.post('/useradmin/users/edit/group_member', dict(username="group_member", groups=[]))
      assert not User.objects.get(username='group_member').groups.filter(name='test-group').exists()

      # Check permissions by logging in as the new user
      c_reg = make_logged_in_client(username=FUNNY_NAME, password="test")
      test_user = User.objects.get(username=FUNNY_NAME)
      test_user.groups.add(Group.objects.get(name="test-group"))
      test_user.save()

      # Regular user should be able to modify oneself
      response = c_reg.post('/useradmin/users/edit/%s' % (FUNNY_NAME_QUOTED,), dict(
          username=FUNNY_NAME,
          first_name="Hello",
          is_active=True,
          groups=[group.id for group in test_user.groups.all()]
          ),
          follow=True
      )
      assert response.status_code == 200
      response = c_reg.get('/useradmin/users/edit/%s' % (FUNNY_NAME_QUOTED,), follow=True)
      assert response.status_code == 200
      assert "Hello" == response.context[0]["form"].instance.first_name
      funny_user = User.objects.get(username=FUNNY_NAME)
      # Can't edit other people.
      response = c_reg.post("/useradmin/users/delete", {u'user_ids': [funny_user.id], 'is_delete': True})
      assert b"You must be a superuser" in response.content, "Regular user can't edit other people"

      # Revert to regular "test" user, that has superuser powers.
      c_su = make_logged_in_client()
      # Inactivate FUNNY_NAME
      c_su.post('/useradmin/users/edit/%s' % (FUNNY_NAME_QUOTED,), dict(
          username=FUNNY_NAME,
          first_name="Hello",
          is_active=False)
      )
      # Now make sure FUNNY_NAME can't log back in
      response = c_reg.get('/useradmin/users/edit/%s' % (FUNNY_NAME_QUOTED,))
      assert response.status_code == 302 and "login" in response["location"], "Inactivated user gets redirected to login page"

      # Create a new user with unicode characters
      response = c.post('/useradmin/users/new', dict(
          username='christian_häusler',
          password1="test",
          password2="test",
          is_active=True
        )
      )
      response = c.get('/useradmin/')
      assert 'christian_häusler' in (response.content if isinstance(response.content, str) else response.content.decode())
      assert len(response.context[0]["users"]) > 1

      # Validate profile is created.
      assert UserProfile.objects.filter(user__username='christian_häusler').exists()

      # Deactivate that regular user
      funny_profile = get_profile(test_user)
      response = c_su.post('/useradmin/users/delete', {u'user_ids': [funny_user.id]})
      assert 302 == response.status_code
      assert User.objects.filter(username=FUNNY_NAME).exists()
      assert UserProfile.objects.filter(id=funny_profile.id).exists()
      assert not User.objects.get(username=FUNNY_NAME).is_active

      # Delete for real
      response = c_su.post('/useradmin/users/delete', {u'user_ids': [funny_user.id], 'is_delete': True})
      assert 302 == response.status_code
      assert not User.objects.filter(username=FUNNY_NAME).exists()
      assert not UserProfile.objects.filter(id=funny_profile.id).exists()

      # Bulk delete users
      u1 = User.objects.create(username='u1', password="u1")
      u2 = User.objects.create(username='u2', password="u2")
      assert User.objects.filter(username__in=['u1', 'u2']).count() == 2
      response = c_su.post('/useradmin/users/delete', {u'user_ids': [u1.id, u2.id], 'is_delete': True})
      assert User.objects.filter(username__in=['u1', 'u2']).count() == 0

      # Make sure that user deletion works if the user has never performed a request.
      funny_user = User.objects.create(username=FUNNY_NAME, password='test')
      assert User.objects.filter(username=FUNNY_NAME).exists()
      assert not UserProfile.objects.filter(user__username=FUNNY_NAME).exists()
      response = c_su.post('/useradmin/users/delete', {u'user_ids': [funny_user.id], 'is_delete': True})
      assert 302 == response.status_code
      assert not User.objects.filter(username=FUNNY_NAME).exists()
      assert not UserProfile.objects.filter(user__username=FUNNY_NAME).exists()

      # You shouldn't be able to create a user without a password
      response = c_su.post('/useradmin/users/new', dict(username="test"))
      assert b"You must specify a password when creating a new user." in response.content
    finally:
      for reset in resets:
        reset()

  def test_deactivate_users(self):
    c = make_logged_in_client('test', is_superuser=True)

    regular_username = 'regular_user'
    regular_user_client = make_logged_in_client(regular_username, is_superuser=True, recreate=True)
    regular_user = User.objects.get(username=regular_username)

    try:
      # Deactivate that regular user
      response = c.post('/useradmin/users/delete', {u'user_ids': [regular_user.id]})
      assert 302 == response.status_code
      assert User.objects.filter(username=regular_username).exists()
      assert not User.objects.get(username=regular_username).is_active

      # Delete for real
      response = c.post('/useradmin/users/delete', {u'user_ids': [regular_user.id], 'is_delete': True})
      assert 302 == response.status_code
      assert not User.objects.filter(username=regular_username).exists()
      assert not UserProfile.objects.filter(id=regular_user.id).exists()
    finally:
      regular_user.delete()

  def test_list_for_autocomplete(self):

    # Now the autocomplete has access to all the users and groups
    c1 = make_logged_in_client('user_test_list_for_autocomplete', is_superuser=False, groupname='group_test_list_for_autocomplete')
    c2_same_group = make_logged_in_client(
      'user_test_list_for_autocomplete2', is_superuser=False, groupname='group_test_list_for_autocomplete'
    )
    c3_other_group = make_logged_in_client(
      'user_test_list_for_autocomplete3', is_superuser=False, groupname='group_test_list_for_autocomplete_other_group'
    )

    # c1 users should list only 'user_test_list_for_autocomplete2' and group should not list 'group_test_list_for_autocomplete_other_group'
    response = c1.get(reverse('useradmin_views_list_for_autocomplete'))
    content = json.loads(response.content)

    users = [smart_str(user['username']) for user in content['users']]
    groups = [smart_str(user['name']) for user in content['groups']]

    assert [u'user_test_list_for_autocomplete2'] == users
    assert u'group_test_list_for_autocomplete' in groups, groups
    assert u'group_test_list_for_autocomplete_other_group' not in groups, groups

    reset = ENABLE_ORGANIZATIONS.set_for_testing(True)
    try:
      response = c1.get(reverse('useradmin_views_list_for_autocomplete'))  # Actually always good as DB created pre-setting flag to True
      assert 200 == response.status_code
    finally:
      reset()

    # only_mygroups has no effect if user is not super user
    response = c1.get(reverse('useradmin_views_list_for_autocomplete'), {'include_myself': True})
    content = json.loads(response.content)

    users = [smart_str(user['username']) for user in content['users']]
    groups = [smart_str(user['name']) for user in content['groups']]

    assert [u'user_test_list_for_autocomplete', u'user_test_list_for_autocomplete2'] == users
    assert u'group_test_list_for_autocomplete' in groups, groups
    assert u'group_test_list_for_autocomplete_other_group' not in groups, groups

    # c3 is alone
    response = c3_other_group.get(reverse('useradmin_views_list_for_autocomplete'), {'include_myself': True})
    content = json.loads(response.content)

    users = [smart_str(user['username']) for user in content['users']]
    groups = [smart_str(user['name']) for user in content['groups']]

    assert [u'user_test_list_for_autocomplete3'] == users
    assert u'group_test_list_for_autocomplete_other_group' in groups, groups

    c4_super_user = make_logged_in_client(is_superuser=True)

    # superuser should get all users as autocomplete filter is not passed
    response = c4_super_user.get('/desktop/api/users/autocomplete', {'include_myself': True, 'only_mygroups': True})
    content = json.loads(response.content)

    users = [smart_str(user['username']) for user in content['users']]
    assert (
      [u'test', u'user_test_list_for_autocomplete', u'user_test_list_for_autocomplete2', u'user_test_list_for_autocomplete3'] == users)

    c5_autocomplete_filter_by_groupname = make_logged_in_client(
      'user_doesnt_match_autocomplete_filter', is_superuser=False, groupname='group_test_list_for_autocomplete'
    )

    # superuser should get all users & groups which match the autocomplete filter case insensitive
    response = c4_super_user.get('/desktop/api/users/autocomplete', {'include_myself': True, 'filter': 'Test_list_for_autocomplete'})
    content = json.loads(response.content)

    users = [smart_str(user['username']) for user in content['users']]
    groups = [smart_str(user['name']) for user in content['groups']]

    assert [u'user_test_list_for_autocomplete', u'user_test_list_for_autocomplete2', u'user_test_list_for_autocomplete3'] == users
    assert [u'group_test_list_for_autocomplete', u'group_test_list_for_autocomplete_other_group'] == groups

  def test_language_preference(self):
    # Test that language selection appears in Edit Profile for current user
    client = make_logged_in_client('test', is_superuser=False, groupname='test')
    user = User.objects.get(username='test')
    grant_access('test', 'test', 'useradmin')

    response = client.get('/useradmin/users/edit/test')
    assert b"Language Preference" in response.content

    # Does not appear for superuser editing other profiles
    other_client = make_logged_in_client('test_super', is_superuser=True, groupname='test')
    superuser = User.objects.get(username='test_super')

    response = other_client.get('/useradmin/users/edit/test')
    assert b"Language Preference" not in response.content, response.content

    # Changing language preference will change language setting
    response = client.post('/useradmin/users/edit/test', dict(language='ko'))
    assert b'<option value="ko" selected>Korean</option>' in response.content

  def test_edit_user_xss(self):
    # Hue 3 Admin
    edit_user = make_logged_in_client('admin', is_superuser=True)
    response = edit_user.post('/useradmin/users/edit/admin', dict(
        username="admin",
        is_superuser=True,
        password1="foo",
        password2="foo",
        language="en-us><script>alert('Hacked')</script>"
        )
    )
    assert (
      b'Select a valid choice. en-us&gt;&lt;script&gt;alert(&#x27;Hacked&#x27;)&lt;/script&gt; '
      b'is not one of the available choices.' in response.content
    )
    # Hue 4 Admin
    response = edit_user.post('/useradmin/users/edit/admin', dict(
        username="admin",
        is_superuser=True,
        language="en-us><script>alert('Hacked')</script>",
        is_embeddable=True)
    )
    content = json.loads(response.content)
    assert 'Select a valid choice. en-us>alert(\'Hacked\') is not one of the available choices.', content['errors'][0]['message'][0]

    # Hue 3, User with access to useradmin app
    edit_user = make_logged_in_client('edit_user', is_superuser=False)
    grant_access('edit_user', 'edit_user', 'useradmin')
    response = edit_user.post('/useradmin/users/edit/edit_user', dict(
        username="edit_user",
        is_superuser=False,
        password1="foo",
        password2="foo",
        language="en-us><script>alert('Hacked')</script>"
        )
    )
    assert (
      b'Select a valid choice. en-us&gt;&lt;script&gt;alert(&#x27;Hacked&#x27;)&lt;/script&gt; '
      b'is not one of the available choices.' in response.content
    )
    # Hue 4, User with access to useradmin app
    response = edit_user.post('/useradmin/users/edit/edit_user', dict(
        username="edit_user",
        is_superuser=False,
        language="en-us><script>alert('Hacked')</script>",
        is_embeddable=True)
    )
    content = json.loads(response.content)
    assert 'Select a valid choice. en-us>alert(\'Hacked\') is not one of the available choices.', content['errors'][0]['message'][0]


@pytest.mark.django_db
@pytest.mark.requires_hadoop
@pytest.mark.integration
class TestUserAdminWithHadoop(BaseUserAdminTests):

  def test_ensure_home_directory(self):
    if not is_live_cluster():
      pytest.skip("Skipping Test")

    resets = [
      useradmin.conf.PASSWORD_POLICY.IS_ENABLED.set_for_testing(False),
    ]

    try:
      reset_password_policy()

      # Cluster and client for home directory creation
      cluster = pseudo_hdfs4.shared_cluster()
      c = make_logged_in_client(cluster.superuser, is_superuser=True, groupname='test1')
      cluster.fs.setuser(cluster.superuser)

      # Create a user with a home directory
      if cluster.fs.exists('/user/test1'):
        cluster.fs.do_as_superuser(cluster.fs.rmtree, '/user/test1')
      assert not cluster.fs.exists('/user/test1')
      response = c.post('/useradmin/users/new', dict(username="test1", password1='test', password2='test', ensure_home_directory=True))
      assert cluster.fs.exists('/user/test1')
      dir_stat = cluster.fs.stats('/user/test1')
      assert 'test1' == dir_stat.user
      assert 'test1' == dir_stat.group
      assert '40755' == '%o' % dir_stat.mode

      # Create a user, then add their home directory
      if cluster.fs.exists('/user/test2'):
        cluster.fs.do_as_superuser(cluster.fs.rmtree, '/user/test2')
      assert not cluster.fs.exists('/user/test2')
      response = c.post('/useradmin/users/new', dict(username="test2", password1='test', password2='test'))
      assert not cluster.fs.exists('/user/test2')
      response = c.post(
        '/useradmin/users/edit/%s' % "test2",
        dict(username="test2", password1='test', password2='test', password_old="test", ensure_home_directory=True)
      )
      assert cluster.fs.exists('/user/test2')
      dir_stat = cluster.fs.stats('/user/test2')
      assert 'test2' == dir_stat.user
      assert 'test2' == dir_stat.group
      assert '40755' == '%o' % dir_stat.mode

      # special character in username ctestë01
      path_with_special_char = '/user/ctestë01'.decode("utf-8")
      if cluster.fs.exists(path_with_special_char):
        cluster.fs.do_as_superuser(cluster.fs.rmtree, path_with_special_char)
      response = c.post('/useradmin/users/new', dict(username='ctestë01', password1='test', password2='test', ensure_home_directory=True))
      assert cluster.fs.exists(path_with_special_char)
      dir_stat = cluster.fs.stats(path_with_special_char)
      assert u'ctestë01' == dir_stat.user
      assert u'ctestë01' == dir_stat.group
      assert '40755' == '%o' % dir_stat.mode
      if cluster.fs.exists(path_with_special_char):  # clean special characters
        cluster.fs.do_as_superuser(cluster.fs.rmtree, path_with_special_char)

      # Ignore domain in username when importing LDAP users
      # eg: Ignore '@ad.sec.cloudera.com' when importing 'test@ad.sec.cloudera.com'
      resets.append(desktop.conf.LDAP.LDAP_URL.set_for_testing('default.example.com'))
      if cluster.fs.exists('/user/test3@ad.sec.cloudera.com'):
        cluster.fs.do_as_superuser(cluster.fs.rmtree, '/user/test3@ad.sec.cloudera.com')
      if cluster.fs.exists('/user/test3'):
        cluster.fs.do_as_superuser(cluster.fs.rmtree, '/user/test3')
      assert not cluster.fs.exists('/user/test3')
      response = c.post(
        '/useradmin/users/new', dict(username="test3@ad.sec.cloudera.com", password1='test', password2='test', ensure_home_directory=True)
      )
      assert not cluster.fs.exists('/user/test3@ad.sec.cloudera.com')
      assert cluster.fs.exists('/user/test3')

      dir_stat = cluster.fs.stats('/user/test3')
      assert 'test3' == dir_stat.user
      assert 'test3' == dir_stat.group
      assert 'test3@ad.sec.cloudera.com' != dir_stat.user
      assert 'test3@ad.sec.cloudera.com' != dir_stat.group
      assert '40755' == '%o' % dir_stat.mode
    finally:
      for reset in resets:
        reset()


class MockLdapConnection(object):
  def __init__(self, ldap_config, ldap_url, username, password, ldap_cert):
    self.ldap_config = ldap_config
    self.ldap_url = ldap_url
    self.username = username
    self.password = password
    self.ldap_cert = ldap_cert


def test_get_connection_bind_password():
  # Unfortunately our tests leak a cached test ldap connection across functions, so we need to clear it out.
  useradmin.ldap_access.CACHED_LDAP_CONN = None

  # Monkey patch the LdapConnection class as we don't want to make a real connection.
  OriginalLdapConnection = useradmin.ldap_access.LdapConnection
  reset = [
      desktop.conf.LDAP.LDAP_URL.set_for_testing('default.example.com'),
      desktop.conf.LDAP.BIND_PASSWORD.set_for_testing('default-password'),
      desktop.conf.LDAP.LDAP_SERVERS.set_for_testing({
        'test': {
          'ldap_url': 'test.example.com',
          'bind_password': 'test-password',
        }
      })
  ]
  try:
    useradmin.ldap_access.LdapConnection = MockLdapConnection

    connection = useradmin.ldap_access.get_connection_from_server()
    assert connection.password == 'default-password'

    connection = useradmin.ldap_access.get_connection_from_server('test')
    assert connection.password == 'test-password'
  finally:
    useradmin.ldap_access.LdapConnection = OriginalLdapConnection
    for f in reset:
      f()


def test_get_connection_bind_password_script():
  # Unfortunately our tests leak a cached test ldap connection across functions, so we need to clear it out.
  useradmin.ldap_access.CACHED_LDAP_CONN = None

  SCRIPT = '%s -c "print(\'\\n password from script \\n\')"' % sys.executable

  # Monkey patch the LdapConnection class as we don't want to make a real connection.
  OriginalLdapConnection = useradmin.ldap_access.LdapConnection
  reset = [
      desktop.conf.LDAP.LDAP_URL.set_for_testing('default.example.com'),
      desktop.conf.LDAP.BIND_PASSWORD_SCRIPT.set_for_testing(
        '%s -c "print(\'\\n default password \\n\')"' % sys.executable
      ),
      desktop.conf.LDAP.LDAP_SERVERS.set_for_testing({
        'test': {
          'ldap_url': 'test.example.com',
          'bind_password_script':
            '%s -c "print(\'\\n test password \\n\')"' % sys.executable,
        }
      })
  ]
  try:
    useradmin.ldap_access.LdapConnection = MockLdapConnection

    connection = useradmin.ldap_access.get_connection_from_server()
    assert connection.password == ' default password '

    connection = useradmin.ldap_access.get_connection_from_server('test')
    assert connection.password == ' test password '
  finally:
    useradmin.ldap_access.LdapConnection = OriginalLdapConnection
    for f in reset:
      f()


class LastActivityMiddlewareTests(object):

  def test_last_activity(self):
    c = make_logged_in_client(username="test", is_superuser=True)
    profile = UserProfile.objects.get(user__username='test')
    assert profile.last_activity != 0

  def test_idle_timeout(self):
    timeout = 5
    reset = [
      desktop.conf.AUTH.IDLE_SESSION_TIMEOUT.set_for_testing(timeout)
    ]
    try:
      c = make_logged_in_client(username="test", is_superuser=True)
      response = c.get(reverse(home))
      assert 200 == response.status_code

      # Assert after timeout that user is redirected to login
      time.sleep(timeout)
      response = c.get(reverse(home))
      assert 302 == response.status_code
    finally:
      for f in reset:
        f()

  def test_ignore_jobbrowser_polling(self):
    timeout = 5
    reset = [
      desktop.conf.AUTH.IDLE_SESSION_TIMEOUT.set_for_testing(timeout)
    ]
    try:
      c = make_logged_in_client(username="test", is_superuser=True)
      response = c.get(reverse(home))
      assert 200 == response.status_code

      # Assert that jobbrowser polling does not reset idle time
      time.sleep(2)
      c.get('jobbrowser/?format=json&state=running&user=%s' % "test")
      time.sleep(3)

      response = c.get(reverse(home))
      assert 302 == response.status_code
    finally:
      for f in reset:
        f()


class ConcurrentUserSessionMiddlewareTests(object):
  def setup_method(self):
    self.cm = ConcurrentUserSessionMiddleware()
    self.reset = desktop.conf.SESSION.CONCURRENT_USER_SESSION_LIMIT.set_for_testing(1)

  def teardown_method(self):
    self.reset()

  def test_concurrent_session_logout(self):
    c = make_logged_in_client(username="test_concurr", groupname="test_concurr", recreate=True, is_superuser=True)
    session = MockSession()
    session.session_key = c.session.session_key
    session.modified = True
    user = MockUser()
    user.id = c.session.get('_auth_user_id')
    user.username = 'test_concurr'
    request = MockRequest()
    request.user = user
    request.session = session
    response = MockRequest()

    # Call middleware with test_concurr on session 1
    self.cm.process_response(request, response)

    c2 = make_logged_in_client(username="test_concurr", groupname="test_concurr", is_superuser=True)
    session.session_key = c2.session.session_key
    request.session = session

    # Call middleware with test_concurr on session 2
    self.cm.process_response(request, response)

    now = datetime.now()
    # Session 1 is expired
    assert list(Session.objects.filter(Q(session_key=c.session.session_key)))[0].expire_date <= now
    assert 302 == c.get('/editor', follow=False).status_code  # Redirect to login page

    # Session 2 is still active
    assert list(Session.objects.filter(Q(session_key=c2.session.session_key)))[0].expire_date > now
    assert 200 == c2.get('/editor', follow=True).status_code
