Name:           freeipa-admintools
Version:        0.4.0
Release:        2%{?dist}
Summary:        FreeIPA authentication server

Group:          System Environment/Base
License:        GPL
URL:            http://www.freeipa.org
Source0:        %{name}-%{version}.tgz
BuildRoot:      %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)
BuildArch: 	noarch

Requires: python freeipa-python

%description
FreeIPA is a server for identity, policy, and audit.

%prep
%setup -q

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}%{_sbindir}

make install DESTDIR=%{buildroot}


%clean
rm -rf %{buildroot}


%files
%defattr(-,root,root,-)
%{_sbindir}/ipa*
%{_mandir}/man1/*

%changelog
* Thu Oct 11 2007 Karl MacMillan <kmacmill@redhat.com> - 0.4.0-2
- Package man files

* Tue Oct  2 2007 Karl MacMillan <kmacmill@redhat.com> - 0.4.0-1
- Milestone 4

* Mon Sep 10 2007 Karl MacMillan <kmacmill@redhat.com> - 0.3.0-1
- Milestone 3

* Fri Aug 17 2007 Karl MacMillan <kmacmill@redhat.com> - 0.2.0-4
- Package additional utilities.

* Mon Aug  5 2007 Rob Crittenden <rcritten@redhat.com> - 0.1.0-3
- Abstracted client class to work directly or over RPC

* Wed Aug  1 2007 Rob Crittenden <rcritten@redhat.com> - 0.1.0-2
- Update tools to do kerberos
- Add User class

* Fri Jul 27 2007 Karl MacMillan <kmacmill@localhost.localdomain> - 0.1.0-1
- Initial rpm version
