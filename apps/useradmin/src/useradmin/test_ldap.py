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
import logging
from unittest.mock import patch

import pytest
from django.conf import settings
from django.urls import reverse

import desktop.conf
from desktop.lib.django_test_util import make_logged_in_client
from desktop.lib.test_utils import grant_access
from hadoop import pseudo_hdfs4
from hadoop.pseudo_hdfs4 import is_live_cluster
from useradmin import ldap_access
from useradmin.models import Group, LdapGroup, User, UserProfile, get_profile
from useradmin.tests import BaseUserAdminTests, LdapTestConnection, create_long_username, reset_all_groups, reset_all_users
from useradmin.views import (
  add_ldap_groups,
  add_ldap_users,
  import_ldap_groups,
  import_ldap_users,
  sync_ldap_groups,
  sync_ldap_users,
  sync_ldap_users_groups,
)

LOG = logging.getLogger()

try:
  import ldap
except ImportError:
  LOG.warning('ldap module is not available')


def get_multi_ldap_config():
  return {'multi_ldap_conf': {
    'users': {},
    'groups': {}
  }}


@pytest.mark.django_db
@pytest.mark.integration
class TestUserAdminLdap(BaseUserAdminTests):
  def test_useradmin_ldap_user_group_membership_sync(self):
    settings.MIDDLEWARE.append('useradmin.middleware.LdapSynchronizationMiddleware')

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()
    # Make sure LDAP groups exist or they won't sync
    import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=False,
      import_members_recursive=False, sync_users=False, import_by_dn=False)
    import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'Test Administrators', import_members=False,
      import_members_recursive=False, sync_users=False, import_by_dn=False)

    reset = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      # Import curly who is part of TestUsers and Test Administrators
      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'curly', sync_groups=False, import_by_dn=False)

      # Set a password so that we can login
      user = User.objects.get(username='curly')
      user.set_password('test')
      user.save()

      # Should have 0 groups
      assert 0 == user.groups.all().count()

      # Make an authenticated request as curly so that we can see call middleware.
      c = make_logged_in_client('curly', 'test', is_superuser=False)
      grant_access("curly", "test", "useradmin")
      response = c.get('/useradmin/users', dict(server='multi_ldap_conf'))

      # Refresh user groups
      user = User.objects.get(username='curly')

      # Should have 3 groups now. 2 from LDAP and 1 from 'grant_access' call.
      assert 3 == user.groups.all().count(), user.groups.all()

      # Now remove a group and try again.
      old_group = ldap_access.CACHED_LDAP_CONN._instance.users['curly']['groups'].pop()

      # Make an authenticated request as curly so that we can see call middleware.
      response = c.get('/useradmin/users', dict(server='multi_ldap_conf'))

      # Refresh user groups
      user = User.objects.get(username='curly')

      # Should have 2 groups now. 1 from LDAP and 1 from 'grant_access' call.
      assert 3 == user.groups.all().count(), user.groups.all()
    finally:
      settings.MIDDLEWARE.remove('useradmin.middleware.LdapSynchronizationMiddleware')

      for finish in reset:
        finish()

  def test_useradmin_ldap_suboordinate_group_integration(self):
    reset = []

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    # Test old subgroups
    reset.append(desktop.conf.LDAP.SUBGROUPS.set_for_testing("suboordinate"))

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      # Import groups only
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=False,
        import_members_recursive=False, sync_users=False, import_by_dn=False)
      test_users = Group.objects.get(name='TestUsers')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 0

      # Import all members of TestUsers
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='TestUsers')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 3

      # Should import a group, but will only sync already-imported members
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'Test Administrators', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert User.objects.all().count() == 3
      assert Group.objects.all().count() == 2
      test_admins = Group.objects.get(name='Test Administrators')
      assert test_admins.user_set.all().count() == 2
      larry = User.objects.get(username='lårry')
      assert test_admins.user_set.all().order_by('username')[1].username == larry.username

      # Only sync already imported
      ldap_access.CACHED_LDAP_CONN.remove_user_group_for_test('uid=moe,ou=People,dc=example,dc=com', 'TestUsers')
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert test_users.user_set.all().count() == 2
      assert User.objects.get(username='moe').groups.all().count() == 0

      # Import missing user
      ldap_access.CACHED_LDAP_CONN.add_user_group_for_test('uid=moe,ou=People,dc=example,dc=com', 'TestUsers')
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert test_users.user_set.all().count() == 3
      assert User.objects.get(username='moe').groups.all().count() == 1

      # Import all members of TestUsers and members of subgroups
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=True,
        import_members_recursive=True, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='TestUsers')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 4

      # Make sure Hue groups with naming collisions don't get marked as LDAP groups
      hue_user = User.objects.create(username='otherguy', first_name='Different', last_name='Guy')
      hue_group = Group.objects.create(name='OtherGroup')
      hue_group.user_set.add(hue_user)
      hue_group.save()
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'OtherGroup', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert not LdapGroup.objects.filter(group=hue_group).exists()
      assert hue_group.user_set.filter(username=hue_user.username).exists()
    finally:
      for finish in reset:
        finish()

  def test_useradmin_ldap_nested_group_integration(self):
    reset = []

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    # Test old subgroups
    reset.append(desktop.conf.LDAP.SUBGROUPS.set_for_testing("nested"))

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      # Import groups only
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=False,
        import_members_recursive=False, sync_users=False, import_by_dn=False)
      test_users = Group.objects.get(name='TestUsers')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 0

      # Import all members of TestUsers
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='TestUsers')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 3

      # Should import a group, but will only sync already-imported members
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'Test Administrators', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert User.objects.all().count() == 3
      assert Group.objects.all().count() == 2
      test_admins = Group.objects.get(name='Test Administrators')
      assert test_admins.user_set.all().count() == 2
      larry = User.objects.get(username='lårry')
      assert test_admins.user_set.all().order_by('username')[1].username == larry.username

      # Only sync already imported
      assert test_users.user_set.all().count() == 3
      ldap_access.CACHED_LDAP_CONN.remove_user_group_for_test('uid=moe,ou=People,dc=example,dc=com', 'TestUsers')
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert test_users.user_set.all().count() == 2
      assert User.objects.get(username='moe').groups.all().count() == 0

      # Import missing user
      ldap_access.CACHED_LDAP_CONN.add_user_group_for_test('uid=moe,ou=People,dc=example,dc=com', 'TestUsers')
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert test_users.user_set.all().count() == 3
      assert User.objects.get(username='moe').groups.all().count() == 1

      # Import all members of TestUsers and not members of suboordinate groups (even though specified)
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=True,
        import_members_recursive=True, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='TestUsers')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 3

      # Nested group import
      # First without recursive import, then with.
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'NestedGroups', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      nested_groups = Group.objects.get(name='NestedGroups')
      nested_group = Group.objects.get(name='NestedGroup')
      assert LdapGroup.objects.filter(group=nested_groups).exists()
      assert LdapGroup.objects.filter(group=nested_group).exists()
      assert nested_groups.user_set.all().count() == 0, nested_groups.user_set.all()
      assert nested_group.user_set.all().count() == 0, nested_group.user_set.all()

      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'NestedGroups', import_members=True,
        import_members_recursive=True, sync_users=True, import_by_dn=False)
      nested_groups = Group.objects.get(name='NestedGroups')
      nested_group = Group.objects.get(name='NestedGroup')
      assert LdapGroup.objects.filter(group=nested_groups).exists()
      assert LdapGroup.objects.filter(group=nested_group).exists()
      assert nested_groups.user_set.all().count() == 0, nested_groups.user_set.all()
      assert nested_group.user_set.all().count() == 1, nested_group.user_set.all()

      # Make sure Hue groups with naming collisions don't get marked as LDAP groups
      hue_user = User.objects.create(username='otherguy', first_name='Different', last_name='Guy')
      hue_group = Group.objects.create(name='OtherGroup')
      hue_group.user_set.add(hue_user)
      hue_group.save()
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'OtherGroup', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert not LdapGroup.objects.filter(group=hue_group).exists()
      assert hue_group.user_set.filter(username=hue_user.username).exists()
    finally:
      for finish in reset:
        finish()

  def test_useradmin_ldap_suboordinate_posix_group_integration(self):
    reset = []

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    # Test old subgroups
    reset.append(desktop.conf.LDAP.SUBGROUPS.set_for_testing("suboordinate"))

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      # Import groups only
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=False,
        import_members_recursive=False, sync_users=False, import_by_dn=False)
      test_users = Group.objects.get(name='PosixGroup')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 0

      # Import all members of TestUsers
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='PosixGroup')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 2

      # Should import a group, but will only sync already-imported members
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'Test Administrators', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert User.objects.all().count() == 2, User.objects.all()
      assert Group.objects.all().count() == 2, Group.objects.all()
      test_admins = Group.objects.get(name='Test Administrators')
      assert test_admins.user_set.all().count() == 1
      larry = User.objects.get(username='lårry')
      assert test_admins.user_set.all()[0].username == larry.username

      # Only sync already imported
      ldap_access.CACHED_LDAP_CONN.remove_posix_user_group_for_test('posix_person', 'PosixGroup')
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert test_users.user_set.all().count() == 1
      assert User.objects.get(username='posix_person').groups.all().count() == 0

      # Import missing user
      ldap_access.CACHED_LDAP_CONN.add_posix_user_group_for_test('posix_person', 'PosixGroup')
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert test_users.user_set.all().count() == 2
      assert User.objects.get(username='posix_person').groups.all().count() == 1

      # Import all members of PosixGroup and members of subgroups
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=True,
        import_members_recursive=True, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='PosixGroup')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 3

      # Make sure Hue groups with naming collisions don't get marked as LDAP groups
      hue_user = User.objects.create(username='otherguy', first_name='Different', last_name='Guy')
      hue_group = Group.objects.create(name='OtherGroup')
      hue_group.user_set.add(hue_user)
      hue_group.save()
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'OtherGroup', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert not LdapGroup.objects.filter(group=hue_group).exists()
      assert hue_group.user_set.filter(username=hue_user.username).exists()
    finally:
      for finish in reset:
        finish()

  def test_useradmin_ldap_nested_posix_group_integration(self):
    reset = []

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    # Test nested groups
    reset.append(desktop.conf.LDAP.SUBGROUPS.set_for_testing("nested"))

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      # Import groups only
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=False,
        import_members_recursive=False, sync_users=False, import_by_dn=False)
      test_users = Group.objects.get(name='PosixGroup')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 0

      # Import all members of TestUsers
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='PosixGroup')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 2

      # Should import a group, but will only sync already-imported members
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'Test Administrators', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert User.objects.all().count() == 2, User.objects.all()
      assert Group.objects.all().count() == 2, Group.objects.all()
      test_admins = Group.objects.get(name='Test Administrators')
      assert test_admins.user_set.all().count() == 1
      larry = User.objects.get(username='lårry')
      assert test_admins.user_set.all()[0].username == larry.username

      # Only sync already imported
      ldap_access.CACHED_LDAP_CONN.remove_posix_user_group_for_test('posix_person', 'PosixGroup')
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert test_users.user_set.all().count() == 1
      assert User.objects.get(username='posix_person').groups.all().count() == 0

      # Import missing user
      ldap_access.CACHED_LDAP_CONN.add_posix_user_group_for_test('posix_person', 'PosixGroup')
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=True,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert test_users.user_set.all().count() == 2
      assert User.objects.get(username='posix_person').groups.all().count() == 1

      # Import all members of PosixGroup and members of subgroups (there should be no subgroups)
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'PosixGroup', import_members=True,
        import_members_recursive=True, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='PosixGroup')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 2

      # Import all members of NestedPosixGroups and members of subgroups
      reset_all_users()
      reset_all_groups()

      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'NestedPosixGroups', import_members=True,
        import_members_recursive=True, sync_users=True, import_by_dn=False)
      test_users = Group.objects.get(name='NestedPosixGroups')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 0
      test_users = Group.objects.get(name='PosixGroup')
      assert LdapGroup.objects.filter(group=test_users).exists()
      assert test_users.user_set.all().count() == 2

      # Make sure Hue groups with naming collisions don't get marked as LDAP groups
      hue_user = User.objects.create(username='otherguy', first_name='Different', last_name='Guy')
      hue_group = Group.objects.create(name='OtherGroup')
      hue_group.user_set.add(hue_user)
      hue_group.save()
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'OtherGroup', import_members=False,
        import_members_recursive=False, sync_users=True, import_by_dn=False)
      assert not LdapGroup.objects.filter(group=hue_group).exists()
      assert hue_group.user_set.filter(username=hue_user.username).exists()
    finally:
      for finish in reset:
        finish()

  def test_useradmin_ldap_user_integration(self):
    if is_live_cluster():
      pytest.skip('HUE-2897: Skipping because the DB may not be case sensitive')

    done = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    done.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
      ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

      # Try importing a user
      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'lårry', sync_groups=False, import_by_dn=False)
      larry = User.objects.get(username='lårry')
      assert larry.first_name == 'Larry'
      assert larry.last_name == 'Stooge'
      assert larry.email == 'larry@stooges.com'
      assert get_profile(larry).creation_method == UserProfile.CreationMethod.EXTERNAL.name

      # Should be a noop
      sync_ldap_users(ldap_access.CACHED_LDAP_CONN)
      sync_ldap_groups(ldap_access.CACHED_LDAP_CONN)
      assert User.objects.all().count() == 1
      assert Group.objects.all().count() == 0

      # Make sure that if a Hue user already exists with a naming collision, we
      # won't overwrite any of that user's information.
      hue_user = User.objects.create(username='otherguy', first_name='Different', last_name='Guy')
      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'otherguy', sync_groups=False, import_by_dn=False)
      hue_user = User.objects.get(username='otherguy')
      assert get_profile(hue_user).creation_method == UserProfile.CreationMethod.HUE.name
      assert hue_user.first_name == 'Different'

      # Make sure LDAP groups exist or they won't sync
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'TestUsers', import_members=False,
        import_members_recursive=False, sync_users=False, import_by_dn=False)
      import_ldap_groups(ldap_access.CACHED_LDAP_CONN, 'Test Administrators', import_members=False,
        import_members_recursive=False, sync_users=False, import_by_dn=False)
      # Try importing a user and sync groups
      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'curly', sync_groups=True, import_by_dn=False, server='multi_ldap_conf')
      curly = User.objects.get(username='curly')
      assert curly.first_name == 'Curly'
      assert curly.last_name == 'Stooge'
      assert curly.email == 'curly@stooges.com'
      assert get_profile(curly).creation_method == UserProfile.CreationMethod.EXTERNAL.name
      assert 2 == curly.groups.all().count(), curly.groups.all()

      reset_all_users()
      reset_all_groups()

      # Test import case sensitivity
      done.append(desktop.conf.LDAP.IGNORE_USERNAME_CASE.set_for_testing(True))
      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'Lårry', sync_groups=False, import_by_dn=False)
      assert not User.objects.filter(username='Lårry').exists()
      assert User.objects.filter(username='lårry').exists()

      # Test lower case
      User.objects.filter(username__iexact='Rock').delete()
      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'Rock', sync_groups=False, import_by_dn=False)
      assert not User.objects.filter(username='Rock').exists()
      assert User.objects.filter(username='rock').exists()

      done.append(desktop.conf.LDAP.FORCE_USERNAME_LOWERCASE.set_for_testing(True))

      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'Rock', sync_groups=False, import_by_dn=False)
      assert not User.objects.filter(username='Rock').exists()
      assert User.objects.filter(username='rock').exists()

      User.objects.filter(username='Rock').delete()
      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'Rock', sync_groups=False, import_by_dn=False)
      assert not User.objects.filter(username='Rock').exists()
      assert User.objects.filter(username='rock').exists()

    finally:
      for finish in done:
        finish()

  def test_useradmin_ldap_force_uppercase(self):
    if is_live_cluster():
      pytest.skip('HUE-2897: Skipping because the DB may not be case sensitive')

    done = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    done.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
      ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

      # Test upper case
      User.objects.filter(username__iexact='Rock').delete()
      done.append(desktop.conf.LDAP.IGNORE_USERNAME_CASE.set_for_testing(False))
      done.append(desktop.conf.LDAP.FORCE_USERNAME_LOWERCASE.set_for_testing(False))
      done.append(desktop.conf.LDAP.FORCE_USERNAME_UPPERCASE.set_for_testing(True))

      import_ldap_users(ldap_access.CACHED_LDAP_CONN, 'Rock', sync_groups=False, import_by_dn=False)
      assert User.objects.filter(username='ROCK').exists()
    finally:
      for finish in done:
        finish()

  def test_add_ldap_users(self):
    if is_live_cluster():
      pytest.skip('HUE-2897: Skipping because the DB may not be case sensitive')

    done = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    done.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      URL = reverse('useradmin:useradmin.views.add_ldap_users')

      # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
      ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

      c = make_logged_in_client('test', is_superuser=True)

      assert c.get(URL)

      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='moe', password1='test', password2='test'))
      assert 'Location' in response, response
      assert '/useradmin/users' in response['Location'], response

      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='bad_name', password1='test', password2='test'))
      assert 'Could not' in response.context[0]['form'].errors['username_pattern'][0], response

      # Test wild card
      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='*rr*', password1='test', password2='test'))
      assert '/useradmin/users' in response['Location'], response

      # Test ignore case
      done.append(desktop.conf.LDAP.IGNORE_USERNAME_CASE.set_for_testing(True))
      User.objects.filter(username='moe').delete()
      assert not User.objects.filter(username='Moe').exists()
      assert not User.objects.filter(username='moe').exists()
      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='Moe', password1='test', password2='test'))
      assert 'Location' in response, response
      assert '/useradmin/users' in response['Location'], response
      assert not User.objects.filter(username='Moe').exists()
      assert User.objects.filter(username='moe').exists()

      # Test lower case
      done.append(desktop.conf.LDAP.FORCE_USERNAME_LOWERCASE.set_for_testing(True))
      User.objects.filter(username__iexact='Rock').delete()
      assert not User.objects.filter(username='Rock').exists()
      assert not User.objects.filter(username='rock').exists()
      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='rock', password1='test', password2='test'))
      assert 'Location' in response, response
      assert '/useradmin/users' in response['Location'], response
      assert not User.objects.filter(username='Rock').exists()
      assert User.objects.filter(username='rock').exists()

      # Test regular with spaces (should fail)
      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='user with space', password1='test', password2='test'))
      assert "Username must not contain whitespaces and ':'" in response.context[0]['form'].errors['username_pattern'][0], response

      # Test dn with spaces in username and dn (should fail)
      response = c.post(
        URL,
        dict(server='multi_ldap_conf', username_pattern='uid=user with space,ou=People,dc=example,dc=com',
          password1='test', password2='test', dn=True)
        )
      assert b"Could not get LDAP details for users in pattern" in response.content, response.content

      # Test dn with spaces in dn, but not username (should succeed)
      response = c.post(
        URL,
        dict(server='multi_ldap_conf', username_pattern='uid=user without space,ou=People,dc=example,dc=com',
          password1='test', password2='test', dn=True)
        )
      assert User.objects.filter(username='spaceless').exists()

    finally:
      for finish in done:
        finish()

  def test_add_ldap_users_force_uppercase(self):
    if is_live_cluster():
      pytest.skip('HUE-2897: Skipping because the DB may not be case sensitive')

    done = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    done.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      URL = reverse('useradmin:useradmin.views.add_ldap_users')

      # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
      ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

      c = make_logged_in_client('test', is_superuser=True)

      assert c.get(URL)

      # Test upper case
      done.append(desktop.conf.LDAP.IGNORE_USERNAME_CASE.set_for_testing(False))
      done.append(desktop.conf.LDAP.FORCE_USERNAME_LOWERCASE.set_for_testing(False))
      done.append(desktop.conf.LDAP.FORCE_USERNAME_UPPERCASE.set_for_testing(True))

      User.objects.filter(username='rock').delete()
      assert not User.objects.filter(username='Rock').exists()
      assert not User.objects.filter(username='ROCK').exists()

      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='Rock', password1='test', password2='test'))
      assert 'Location' in response, response
      assert '/useradmin/users' in response['Location'], response
      assert User.objects.filter(username='ROCK').exists()
    finally:
      for finish in done:
        finish()

  def test_ldap_import_truncate_first_last_name(self):
    test_ldap_data = [(
      'uid=testuser,ou=people,dc=sec,dc=test,dc=com',
      {
        'objectClass': ['inetOrgPerson', 'posixAccount', 'shadowAccount'],
        'mail': ['testuser@sec.test.com'],
        'givenName': ['Firstnamehasmorethanthirtycharacters'],
        'uid': ['testuser'],
        'sn': ['Lastnamehasmorethanthirtycharacters']
      }
    )]

    # Checking if first/last name truncation works for LDAP imports
    user_info = ldap_access.LdapConnection._transform_find_user_results(result_data=test_ldap_data, user_name_attr='uid')
    assert not len(user_info[0]['first']) > 30
    assert not len(user_info[0]['last']) > 30
    assert user_info[0]['first'] == 'Firstnamehasmorethanthirtychar', user_info[0]['first']
    assert user_info[0]['last'] == 'Lastnamehasmorethanthirtychara', user_info[0]['last']

    test_ldap_data = [(
      'uid=thaiuser,ou=people,dc=sec,dc=test,dc=com',
      {
        'objectClass': ['inetOrgPerson', 'posixAccount', 'shadowAccount'],
        'mail': ['thaiuser@sec.test.com'],
        'givenName': ['ดีหรือแย่ อย่าไปแคร์ คนนินทา'],
        'uid': ['thaiuser'],
        'sn': ['ชมหรือด่า อย่าไปรับ ให้กลับคืนไป']
      }
    )]

    # Checking if first/last name in Thai truncation works for LDAP imports
    user_info = ldap_access.LdapConnection._transform_find_user_results(result_data=test_ldap_data, user_name_attr='uid')
    assert not len(user_info[0]['first']) > 30
    assert not len(user_info[0]['last']) > 30
    good_first_name = u'ดีหรือแย่ อย่าไปแคร์ คนนินทา'
    truncated_last_name = u'ชมหรือด่า อย่าไปรับ ให้กลับคืนไป'[:30]
    assert user_info[0]['first'], good_first_name
    assert user_info[0]['last'], truncated_last_name

    user, created = ldap_access.get_or_create_ldap_user(username=user_info[0]['username'])
    user.first_name = user_info[0]['first']
    user.last_name = 'ชมหรือด่า อย่าไปรับ ให้กลับคืนไป'[:30]

    user.last_name = user_info[0]['last']
    user.save()
    assert user.first_name, good_first_name
    assert user.last_name, truncated_last_name

  def test_add_ldap_groups(self):
    URL = reverse('useradmin:useradmin.views.add_ldap_groups')

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    c = make_logged_in_client(username='test', is_superuser=True)

    reset = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:

      assert c.get(URL)

      response = c.post(URL, dict(server='multi_ldap_conf', groupname_pattern='TestUsers'))
      assert 'Location' in response, response
      assert '/useradmin/groups' in response['Location']

      # Test warning notification for failed users on group import
      # Import test_longfirstname user
      ldap_access.CACHED_LDAP_CONN.add_user_group_for_test('uid=test_longfirstname,ou=People,dc=example,dc=com', 'TestUsers')
      response = c.post(URL, dict(server='multi_ldap_conf', groupname_pattern='TestUsers', import_members=True), follow=True)

      user_list_a = create_long_username().encode('utf-8') + b", test_longfirstname"
      user_list_b = b"test_longfirstname, " + create_long_username().encode('utf-8')

      assert (b'Failed to import following users: %s' % user_list_a in response.content
        or b'Failed to import following users: %s' % user_list_b in response.content), response.content

      # Test with space
      response = c.post(URL, dict(server='multi_ldap_conf', groupname_pattern='Test Administrators'))
      assert 'Location' in response, response
      assert '/useradmin/groups' in response['Location'], response

      response = c.post(URL, dict(server='multi_ldap_conf', groupname_pattern='toolongnametoolongnametoolongnametoolongname'
                                                                       'toolongnametoolongnametoolongnametoolongname'
                                                                       'toolongnametoolongnametoolongnametoolongname'
                                                                       'toolongnametoolongnametoolongnametoolongname'
                                                                       'toolongnametoolongnametoolongnametoolongname'
                                                                       'toolongnametoolongnametoolongnametoolongname'))
      assert 'Ensure this value has at most 256 characters' in response.context[0]['form'].errors['groupname_pattern'][0], response

      # Test wild card
      response = c.post(URL, dict(server='multi_ldap_conf', groupname_pattern='*r*'))
      assert '/useradmin/groups' in response['Location'], response
    finally:
      for finish in reset:
        finish()

  def test_sync_ldap_users_groups(self):
    URL = reverse('useradmin:useradmin_views_sync_ldap_users_groups')

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    c = make_logged_in_client('test', is_superuser=True)

    reset = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      assert c.get(URL)
      assert c.post(URL)
    finally:
      for finish in reset:
        finish()

  def test_ldap_exception_handling(self):
    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    c = make_logged_in_client('test', is_superuser=True)

    reset = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      with patch('useradmin.test_ldap.LdapTestConnection.find_users') as find_users:
        find_users.side_effect = ldap.LDAPError('No such object')
        response = c.post(
          reverse('useradmin:useradmin.views.add_ldap_users'),
          dict(server='multi_ldap_conf', username_pattern='moe', password1='test', password2='test'),
          follow=True
        )
        assert b'There was an error when communicating with LDAP' in response.content, response
    finally:
      for finish in reset:
        finish()


@pytest.mark.django_db
@pytest.mark.requires_hadoop
@pytest.mark.integration
class TestUserAdminLdapWithHadoop(BaseUserAdminTests):

  def test_ensure_home_directory_add_ldap_users(self):
    URL = reverse('useradmin:useradmin.views.add_ldap_users')

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    cluster = pseudo_hdfs4.shared_cluster()
    c = make_logged_in_client(cluster.superuser, is_superuser=True)
    cluster.fs.setuser(cluster.superuser)

    reset = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      assert c.get(URL)

      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='moe', password1='test', password2='test'))
      assert '/useradmin/users' in response['Location']
      assert not cluster.fs.exists('/user/moe')

      # Try same thing with home directory creation.
      response = c.post(
        URL, dict(server='multi_ldap_conf', username_pattern='curly', password1='test', password2='test', ensure_home_directory=True)
      )
      assert '/useradmin/users' in response['Location']
      assert cluster.fs.exists('/user/curly')

      response = c.post(URL, dict(server='multi_ldap_conf', username_pattern='bad_name', password1='test', password2='test'))
      assert 'Could not' in response.context[0]['form'].errors['username_pattern'][0]
      assert not cluster.fs.exists('/user/bad_name')

      # See if moe, who did not ask for his home directory, has a home directory.
      assert not cluster.fs.exists('/user/moe')

      # Try wild card now
      response = c.post(
        URL, dict(server='multi_ldap_conf', username_pattern='*rr*', password1='test', password2='test', ensure_home_directory=True)
      )
      assert '/useradmin/users' in response['Location']
      assert cluster.fs.exists('/user/curly')
      assert cluster.fs.exists(u'/user/lårry')
      assert not cluster.fs.exists('/user/otherguy')
    finally:
      # Clean up
      for finish in reset:
        finish()

      if cluster.fs.exists('/user/curly'):
        cluster.fs.rmtree('/user/curly')
      if cluster.fs.exists(u'/user/lårry'):
        cluster.fs.rmtree(u'/user/lårry')
      if cluster.fs.exists('/user/otherguy'):
        cluster.fs.rmtree('/user/otherguy')

  def test_ensure_home_directory_sync_ldap_users_groups(self):
    URL = reverse('useradmin:useradmin_views_sync_ldap_users_groups')

    # Set up LDAP tests to use a LdapTestConnection instead of an actual LDAP connection
    ldap_access.CACHED_LDAP_CONN = LdapTestConnection()

    cluster = pseudo_hdfs4.shared_cluster()
    c = make_logged_in_client(cluster.superuser, is_superuser=True)
    cluster.fs.setuser(cluster.superuser)

    reset = []

    # Set to nonsensical value just to force new config usage.
    # Should continue to use cached connection.
    reset.append(desktop.conf.LDAP.LDAP_SERVERS.set_for_testing(get_multi_ldap_config()))

    try:
      c.post(
        reverse('useradmin:useradmin.views.add_ldap_users'),
        dict(server='multi_ldap_conf', username_pattern='curly', password1='test', password2='test')
      )
      assert not cluster.fs.exists('/user/curly')
      assert c.post(URL, dict(server='multi_ldap_conf', ensure_home_directory=True))
      assert cluster.fs.exists('/user/curly')
    finally:
      for finish in reset:
        finish()

      if cluster.fs.exists('/user/curly'):
        cluster.fs.rmtree('/user/curly')
