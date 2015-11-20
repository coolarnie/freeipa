#
# Copyright (C) 2015  FreeIPA Contributors see COPYING for license
#

from __future__ import print_function

import dns.exception as dnsexception
import dns.name as dnsname
import dns.resolver as dnsresolver
import dns.reversename as dnsreversename
import os
import shutil
import socket
import sys
import tempfile

from ipapython import certmonger, ipaldap, ipautil, sysrestore
from ipapython.dn import DN
from ipapython.install import common, core
from ipapython.install.common import step
from ipapython.install.core import Knob
from ipapython.ipa_log_manager import root_logger
from ipaplatform import services
from ipaplatform.tasks import tasks
from ipaplatform.paths import paths
from ipalib import api, certstore, constants, create_api, errors, x509
import ipaclient.ipachangeconf
import ipaclient.ntpconf
from ipaserver.install import (
    bindinstance, ca, cainstance, certs, dns, dsinstance, httpinstance,
    installutils, kra, krainstance, krbinstance, memcacheinstance,
    ntpinstance, otpdinstance, custodiainstance, service)
from ipaserver.install.installutils import create_replica_config
from ipaserver.install.installutils import ReplicaConfig
from ipaserver.install.replication import (
    ReplicationManager, replica_conn_check)
import SSSDConfig
from subprocess import CalledProcessError
from binascii import hexlify

from .common import BaseServer

DIRMAN_DN = DN(('cn', 'directory manager'))


def get_dirman_password():
    return installutils.read_password("Directory Manager (existing master)",
                                      confirm=False, validate=False)


def make_pkcs12_info(directory, cert_name, password_name):
    """Make pkcs12_info

    :param directory: Base directory (config.dir)
    :param cert_name: Cert filename (e.g. "dscert.p12")
    :param password_name: Cert filename (e.g. "dirsrv_pin.txt")
    :return: a (full cert path, password) tuple, or None if cert is not found
    """
    cert_path = os.path.join(directory, cert_name)
    if ipautil.file_exists(cert_path):
        password_file = os.path.join(directory, password_name)
        password = open(password_file).read().strip()
        return cert_path, password
    else:
        return None


def install_http_certs(config, fstore):

    # Obtain keytab for the HTTP service
    fstore.backup_file(paths.IPA_KEYTAB)
    try:
        os.unlink(paths.IPA_KEYTAB)
    except OSError:
        pass

    principal = 'HTTP/%s@%s' % (config.host_name, config.realm_name)
    installutils.install_service_keytab(principal,
                                        config.master_host_name,
                                        paths.IPA_KEYTAB)

    # Obtain certificate for the HTTP service
    nssdir = certs.NSS_DIR
    subject = DN(('O', config.realm_name))
    db = certs.CertDB(config.realm_name, nssdir=nssdir, subject_base=subject)
    db.request_service_cert('Server-Cert', principal, config.host_name, True)
    # FIXME: need Signing-Cert too ?


def install_replica_ds(config, options, promote=False):
    dsinstance.check_ports()

    # if we have a pkcs12 file, create the cert db from
    # that. Otherwise the ds setup will create the CA
    # cert
    pkcs12_info = make_pkcs12_info(config.dir, "dscert.p12", "dirsrv_pin.txt")

    ds = dsinstance.DsInstance(
        config_ldif=options.dirsrv_config_file)
    ds.create_replica(
        realm_name=config.realm_name,
        master_fqdn=config.master_host_name,
        fqdn=config.host_name,
        domain_name=config.domain_name,
        dm_password=config.dirman_password,
        subject_base=config.subject_base,
        pkcs12_info=pkcs12_info,
        ca_is_configured=ipautil.file_exists(config.dir + "/cacert.p12"),
        ca_file=config.dir + "/ca.crt",
        promote=promote,
    )

    return ds


def install_krb(config, setup_pkinit=False, promote=False):
    krb = krbinstance.KrbInstance()

    # pkinit files
    pkcs12_info = make_pkcs12_info(config.dir, "pkinitcert.p12",
                                   "pkinit_pin.txt")

    krb.create_replica(config.realm_name,
                       config.master_host_name, config.host_name,
                       config.domain_name, config.dirman_password,
                       setup_pkinit, pkcs12_info, promote=promote)

    return krb


def install_ca_cert(ldap, base_dn, realm, cafile):
    try:
        try:
            certs = certstore.get_ca_certs(ldap, base_dn, realm, False)
        except errors.NotFound:
            shutil.copy(cafile, constants.CACERT)
        else:
            certs = [c[0] for c in certs if c[2] is not False]
            x509.write_certificate_list(certs, constants.CACERT)

        os.chmod(constants.CACERT, 0o444)
    except Exception as e:
        print("error copying files: " + str(e))
        sys.exit(1)


def install_http(config, auto_redirect, promote=False):
    # if we have a pkcs12 file, create the cert db from
    # that. Otherwise the ds setup will create the CA
    # cert
    pkcs12_info = make_pkcs12_info(config.dir, "httpcert.p12", "http_pin.txt")

    memcache = memcacheinstance.MemcacheInstance()
    memcache.create_instance('MEMCACHE', config.host_name,
                             config.dirman_password,
                             ipautil.realm_to_suffix(config.realm_name))

    http = httpinstance.HTTPInstance()
    http.create_instance(
        config.realm_name, config.host_name, config.domain_name,
        config.dirman_password, False, pkcs12_info,
        auto_redirect=auto_redirect, ca_file=config.dir + "/ca.crt",
        ca_is_configured=ipautil.file_exists(config.dir + "/cacert.p12"),
        promote=promote)

    http.setup_firefox_extension(config.realm_name, config.domain_name)

    return http


def install_dns_records(config, options, remote_api):

    if not bindinstance.dns_container_exists(
            config.host_name,
            ipautil.realm_to_suffix(config.realm_name),
            realm=config.realm_name, ldapi=True,
            autobind=ipaldap.AUTOBIND_ENABLED):
        return

    try:
        bind = bindinstance.BindInstance(dm_password=config.dirman_password,
                                         api=remote_api)
        for ip in config.ips:
            reverse_zone = bindinstance.find_reverse_zone(ip, remote_api)

            bind.add_master_dns_records(config.host_name,
                                        str(ip),
                                        config.realm_name,
                                        config.domain_name,
                                        reverse_zone,
                                        not options.no_ntp,
                                        options.setup_ca)
    except errors.NotFound as e:
        root_logger.debug('Replica DNS records could not be added '
                          'on master: %s', str(e))

    # we should not fail here no matter what
    except Exception as e:
        root_logger.info('Replica DNS records could not be added '
                         'on master: %s', str(e))


def check_dirsrv():
    (ds_unsecure, ds_secure) = dsinstance.check_ports()
    if not ds_unsecure or not ds_secure:
        print("IPA requires ports 389 and 636 for the Directory Server.")
        print("These are currently in use:")
        if not ds_unsecure:
            print("\t389")
        if not ds_secure:
            print("\t636")
        sys.exit(1)


def check_dns_resolution(host_name, dns_servers):
    """Check forward and reverse resolution of host_name using dns_servers
    """
    # Point the resolver at specified DNS server
    server_ips = []
    for dns_server in dns_servers:
        try:
            server_ips = list(
                a[4][0] for a in socket.getaddrinfo(dns_server, None))
        except socket.error:
            pass
        else:
            break
    if not server_ips:
        root_logger.error(
            'Could not resolve any DNS server hostname: %s', dns_servers)
        return False
    resolver = dnsresolver.Resolver()
    resolver.nameservers = server_ips

    root_logger.debug('Search DNS server %s (%s) for %s',
                      dns_server, server_ips, host_name)

    # Get IP addresses of host_name
    addresses = set()
    for rtype in 'A', 'AAAA':
        try:
            result = resolver.query(host_name, rtype)
        except dnsexception.DNSException:
            rrset = []
        else:
            rrset = result.rrset
        if rrset:
            addresses.update(r.address for r in result.rrset)

    if not addresses:
        root_logger.error(
            'Could not resolve hostname %s using DNS. '
            'Clients may not function properly. '
            'Please check your DNS setup. '
            '(Note that this check queries IPA DNS directly and '
            'ignores /etc/hosts.)',
            host_name)
        return False

    no_errors = True

    # Check each of the IP addresses
    checked = set()
    for address in addresses:
        if address in checked:
            continue
        checked.add(address)
        try:
            root_logger.debug('Check reverse address %s (%s)',
                              address, host_name)
            revname = dnsreversename.from_address(address)
            rrset = resolver.query(revname, 'PTR').rrset
        except Exception as e:
            root_logger.debug('Check failed: %s %s', type(e).__name__, e)
            root_logger.error(
                'Reverse DNS resolution of address %s (%s) failed. '
                'Clients may not function properly. '
                'Please check your DNS setup. '
                '(Note that this check queries IPA DNS directly and '
                'ignores /etc/hosts.)',
                address, host_name)
            no_errors = False
        else:
            host_name_obj = dnsname.from_text(host_name)
            if rrset:
                names = [r.target.to_text() for r in rrset]
            else:
                names = []
            root_logger.debug(
                'Address %s resolves to: %s. ', address, ', '.join(names))
            if not rrset or not any(
                    r.target == host_name_obj for r in rrset):
                root_logger.error(
                    'The IP address %s of host %s resolves to: %s. '
                    'Clients may not function properly. '
                    'Please check your DNS setup. '
                    '(Note that this check queries IPA DNS directly and '
                    'ignores /etc/hosts.)',
                    address, host_name, ', '.join(names))
                no_errors = False

    return no_errors


def check_ca_enabled(api):
    try:
        api.Backend.rpcclient.connect()
        result = api.Backend.rpcclient.forward(
            'ca_is_enabled',
            version=u'2.112'    # All the way back to 3.0 servers
        )
        return result['result']
    finally:
        if api.Backend.rpcclient.isconnected():
            api.Backend.rpcclient.disconnect()


def configure_certmonger():
    messagebus = services.knownservices.messagebus
    try:
        messagebus.start()
    except Exception, e:
        print("Messagebus service unavailable: %s" % str(e))
        sys.exit(3)

    # Ensure that certmonger has been started at least once to generate the
    # cas files in /var/lib/certmonger/cas.
    cmonger = services.knownservices.certmonger
    try:
        cmonger.restart()
    except Exception, e:
        print("Certmonger service unavailable: %s" % str(e))
        sys.exit(3)

    try:
        cmonger.enable()
    except Exception, e:
        print("Failed to enable Certmonger: %s" % str(e))
        sys.exit(3)


def remove_replica_info_dir(installer):
    # always try to remove decrypted replica file
    try:
        if installer._top_dir is not None:
            shutil.rmtree(installer._top_dir)
    except OSError:
        pass


def common_cleanup(func):
    def decorated(installer):
        try:
            try:
                func(installer)
            except BaseException:
                remove_replica_info_dir(installer)
                raise
        except KeyboardInterrupt:
            sys.exit(1)
        except Exception:
            print(
                "Your system may be partly configured.\n"
                "Run /usr/sbin/ipa-server-install --uninstall to clean up.\n")
            raise

    return decorated


def promote_sssd(host_name):
    sssdconfig = SSSDConfig.SSSDConfig()
    sssdconfig.import_config()
    domains = sssdconfig.list_active_domains()

    ipa_domain = None

    for name in domains:
        domain = sssdconfig.get_domain(name)
        try:
            hostname = domain.get_option('ipa_hostname')
            if hostname == host_name:
                ipa_domain = domain
        except SSSDConfig.NoOptionError:
            continue

    if ipa_domain is None:
        raise RuntimeError("Couldn't find IPA domain in sssd.conf")
    else:
        domain.set_option('ipa_server', host_name)
        domain.set_option('ipa_server_mode', True)
        sssdconfig.save_domain(domain)
        sssdconfig.write()

        sssd = services.service('sssd')
        try:
            sssd.restart()
        except CalledProcessError:
            root_logger.warning("SSSD service restart was unsuccessful.")


@common_cleanup
def install_check(installer):
    options = installer
    filename = installer.replica_file

    tasks.check_selinux_status()

    client_fstore = sysrestore.FileStore(paths.IPA_CLIENT_SYSRESTORE)
    if client_fstore.has_files():
        sys.exit("IPA client is already configured on this system.\n"
                 "Please uninstall it first before configuring the replica, "
                 "using 'ipa-client-install --uninstall'.")

    sstore = sysrestore.StateFile(paths.SYSRESTORE)

    fstore = sysrestore.FileStore(paths.SYSRESTORE)

    # Check to see if httpd is already configured to listen on 443
    if httpinstance.httpd_443_configured():
        sys.exit("Aborting installation")

    check_dirsrv()

    if not options.no_ntp:
        try:
            ipaclient.ntpconf.check_timedate_services()
        except ipaclient.ntpconf.NTPConflictingService as e:
            print(("WARNING: conflicting time&date synchronization service '%s'"
                  " will" % e.conflicting_service))
            print("be disabled in favor of ntpd")
            print("")
        except ipaclient.ntpconf.NTPConfigurationError:
            pass

    # get the directory manager password
    dirman_password = options.password
    if not dirman_password:
        try:
            dirman_password = get_dirman_password()
        except KeyboardInterrupt:
            sys.exit(0)
        if dirman_password is None:
            sys.exit("Directory Manager password required")

    config = create_replica_config(dirman_password, filename, options)
    installer._top_dir = config.top_dir
    config.setup_ca = options.setup_ca
    config.setup_kra = options.setup_kra

    # Create the management framework config file
    # Note: We must do this before bootstraping and finalizing ipalib.api
    old_umask = os.umask(0o22)   # must be readable for httpd
    try:
        fd = open(paths.IPA_DEFAULT_CONF, "w")
        fd.write("[global]\n")
        fd.write("host=%s\n" % config.host_name)
        fd.write("basedn=%s\n" %
                 str(ipautil.realm_to_suffix(config.realm_name)))
        fd.write("realm=%s\n" % config.realm_name)
        fd.write("domain=%s\n" % config.domain_name)
        fd.write("xmlrpc_uri=https://%s/ipa/xml\n" %
                 ipautil.format_netloc(config.host_name))
        fd.write("ldap_uri=ldapi://%%2fvar%%2frun%%2fslapd-%s.socket\n" %
                 installutils.realm_to_serverid(config.realm_name))
        if ipautil.file_exists(config.dir + "/cacert.p12"):
            fd.write("enable_ra=True\n")
            fd.write("ra_plugin=dogtag\n")
            fd.write("dogtag_version=10\n")
        else:
            fd.write("enable_ra=False\n")
            fd.write("ra_plugin=none\n")

        fd.write("mode=production\n")
        fd.close()
    finally:
        os.umask(old_umask)

    api.bootstrap(in_server=True, context='installer')
    api.finalize()

    installutils.verify_fqdn(config.master_host_name, options.no_host_dns)

    cafile = config.dir + "/ca.crt"
    if not ipautil.file_exists(cafile):
        raise RuntimeError("CA cert file is not available. Please run "
                           "ipa-replica-prepare to create a new replica file.")

    ldapuri = 'ldaps://%s' % ipautil.format_netloc(config.master_host_name)
    remote_api = create_api(mode=None)
    remote_api.bootstrap(in_server=True, context='installer',
                         ldap_uri=ldapuri)
    remote_api.finalize()
    conn = remote_api.Backend.ldap2
    replman = None
    try:
        # Try out the password
        conn.connect(bind_dn=DIRMAN_DN, bind_pw=config.dirman_password,
                     tls_cacertfile=cafile)
        replman = ReplicationManager(config.realm_name,
                                     config.master_host_name,
                                     config.dirman_password)

        # Check that we don't already have a replication agreement
        if replman.get_replication_agreement(config.host_name):
            root_logger.info('Error: A replication agreement for this '
                             'host already exists.')
            print('A replication agreement for this host already exists. '
                  'It needs to be removed.')
            print("Run this on the master that generated the info file:")
            print(("    %% ipa-replica-manage del %s --force" %
                  config.host_name))
            sys.exit(3)

        # Detect the current domain level
        try:
            current = remote_api.Command['domainlevel_get']()['result']
        except errors.NotFound:
            # If we're joining an older master, domain entry is not
            # available
            current = constants.DOMAIN_LEVEL_0

        if current != constants.DOMAIN_LEVEL_0:
            raise RuntimeError(
                "You cannot use a replica file to join a replica when the "
                "domain is above level 0. Please join the system to the "
                "domain by running ipa-client-install first, the try again "
                "without a replica file."
            )

        # Detect if current level is out of supported range
        # for this IPA version
        under_lower_bound = current < constants.MIN_DOMAIN_LEVEL
        above_upper_bound = current > constants.MAX_DOMAIN_LEVEL

        if under_lower_bound or above_upper_bound:
            message = ("This version of FreeIPA does not support "
                       "the Domain Level which is currently set for "
                       "this domain. The Domain Level needs to be "
                       "raised before installing a replica with "
                       "this version is allowed to be installed "
                       "within this domain.")
            root_logger.error(message)
            print(message)
            sys.exit(3)

        # Check pre-existing host entry
        try:
            entry = conn.find_entries(u'fqdn=%s' % config.host_name,
                                      ['fqdn'], DN(api.env.container_host,
                                                   api.env.basedn))
        except errors.NotFound:
            pass
        else:
            root_logger.info('Error: Host %s already exists on the master '
                             'server.' % config.host_name)
            print(('The host %s already exists on the master server.' %
                  config.host_name))
            print("You should remove it before proceeding:")
            print("    %% ipa host-del %s" % config.host_name)
            sys.exit(3)

        dns_masters = remote_api.Object['dnsrecord'].get_dns_masters()
        if dns_masters:
            if not options.no_host_dns:
                master = config.master_host_name
                root_logger.debug('Check forward/reverse DNS resolution')
                resolution_ok = (
                    check_dns_resolution(master, dns_masters) and
                    check_dns_resolution(config.host_name, dns_masters))
                if not resolution_ok and installer.interactive:
                    if not ipautil.user_input("Continue?", False):
                        sys.exit(0)
        else:
            root_logger.debug('No IPA DNS servers, '
                              'skipping forward/reverse resolution check')

        if options.setup_ca:
            options.realm_name = config.realm_name
            options.host_name = config.host_name
            options.subject = config.subject_base
            ca.install_check(False, config, options)

        if config.setup_kra:
            try:
                kra.install_check(remote_api, config, options)
            except RuntimeError as e:
                print(str(e))
                sys.exit(1)
    except errors.ACIError:
        sys.exit("\nThe password provided is incorrect for LDAP server "
                 "%s" % config.master_host_name)
    except errors.LDAPError:
        sys.exit("\nUnable to connect to LDAP server %s" %
                 config.master_host_name)
    finally:
        if replman and replman.conn:
            replman.conn.unbind()
        if conn.isconnected():
            conn.disconnect()

    if options.setup_dns:
        dns.install_check(False, True, options, config.host_name)
        config.ips = dns.ip_addresses
    else:
        config.ips = installutils.get_server_ip_address(
            config.host_name, not installer.interactive, False,
            options.ip_addresses)

    # installer needs to update hosts file when DNS subsystem will be
    # installed or custom addresses are used
    if options.setup_dns or options.ip_addresses:
        installer._update_hosts_file = True

    # check connection
    if not options.skip_conncheck:
        replica_conn_check(
            config.master_host_name, config.host_name, config.realm_name,
            options.setup_ca, config.ca_ds_port, options.admin_password)

    installer._remote_api = remote_api
    installer._fstore = fstore
    installer._sstore = sstore
    installer._config = config


@common_cleanup
def install(installer):
    options = installer
    fstore = installer._fstore
    sstore = installer._sstore
    config = installer._config

    if installer._update_hosts_file:
        installutils.update_hosts_file(config.ips, config.host_name, fstore)

    # Create DS user/group if it doesn't exist yet
    dsinstance.create_ds_user()

    cafile = config.dir + "/ca.crt"

    remote_api = installer._remote_api
    conn = remote_api.Backend.ldap2
    try:
        conn.connect(bind_dn=DIRMAN_DN, bind_pw=config.dirman_password,
                     tls_cacertfile=cafile)

        # Install CA cert so that we can do SSL connections with ldap
        install_ca_cert(conn, api.env.basedn, api.env.realm, cafile)

        # Configure ntpd
        if not options.no_ntp:
            ipaclient.ntpconf.force_ntpd(sstore)
            ntp = ntpinstance.NTPInstance()
            ntp.create_instance()

        # Configure dirsrv
        ds = install_replica_ds(config, options)

        # Always try to install DNS records
        install_dns_records(config, options, remote_api)
    finally:
        if conn.isconnected():
            conn.disconnect()

    options.dm_password = config.dirman_password

    if config.setup_ca:
        options.realm_name = config.realm_name
        options.domain_name = config.domain_name
        options.host_name = config.host_name

        if ipautil.file_exists(config.dir + "/cacert.p12"):
            options.ra_p12 = config.dir + "/ra.p12"

        ca.install(False, config, options)

    krb = install_krb(config, setup_pkinit=not options.no_pkinit)
    http = install_http(config, auto_redirect=not options.no_ui_redirect)

    otpd = otpdinstance.OtpdInstance()
    otpd.create_instance('OTPD', config.host_name, config.dirman_password,
                         ipautil.realm_to_suffix(config.realm_name))

    if ipautil.file_exists(config.dir + "/cacert.p12"):
        CA = cainstance.CAInstance(config.realm_name, certs.NSS_DIR)
        CA.dm_password = config.dirman_password

        CA.configure_certmonger_renewal()
        CA.import_ra_cert(config.dir + "/ra.p12")
        CA.fix_ra_perms()

    custodia = custodiainstance.CustodiaInstance(config.host_name,
                                                 config.realm_name)
    custodia.create_instance(config.dirman_password)

    # The DS instance is created before the keytab, add the SSL cert we
    # generated
    ds.add_cert_to_service()

    # Apply any LDAP updates. Needs to be done after the replica is synced-up
    service.print_msg("Applying LDAP updates")
    ds.apply_updates()

    if options.setup_kra:
        kra.install(api, config, options)
    else:
        service.print_msg("Restarting the directory server")
        ds.restart()

    service.print_msg("Restarting the KDC")
    krb.restart()

    if config.setup_ca:
        services.knownservices['pki_tomcatd'].restart('pki-tomcat')

    if options.setup_dns:
        api.Backend.ldap2.connect(autobind=True)
        dns.install(False, True, options)

    # Restart httpd to pick up the new IPA configuration
    service.print_msg("Restarting the web server")
    http.restart()

    # Call client install script
    service.print_msg("Configuring client side components")
    try:
        args = [paths.IPA_CLIENT_INSTALL, "--on-master", "--unattended",
                "--domain", config.domain_name, "--server", config.host_name,
                "--realm", config.realm_name]
        if options.no_dns_sshfp:
            args.append("--no-dns-sshfp")
        if options.ssh_trust_dns:
            args.append("--ssh-trust-dns")
        if options.no_ssh:
            args.append("--no-ssh")
        if options.no_sshd:
            args.append("--no-sshd")
        if options.mkhomedir:
            args.append("--mkhomedir")
        ipautil.run(args)
    except Exception as e:
        print("Configuration of client side components failed!")
        print("ipa-client-install returned: " + str(e))
        raise RuntimeError("Failed to configure the client")

    ds.replica_populate()

    # Everything installed properly, activate ipa service.
    services.knownservices.ipa.enable()

    remove_replica_info_dir(installer)


@common_cleanup
def promote_check(installer):
    options = installer

    installer._top_dir = tempfile.mkdtemp("ipa")

    tasks.check_selinux_status()

    client_fstore = sysrestore.FileStore(paths.IPA_CLIENT_SYSRESTORE)
    if not client_fstore.has_files():
        sys.exit("IPA client is not configured on this system.\n"
                 "You must use a replica file or join the system "
                 "using 'ipa-client-install'.")

    sstore = sysrestore.StateFile(paths.SYSRESTORE)

    fstore = sysrestore.FileStore(paths.SYSRESTORE)

    # Check to see if httpd is already configured to listen on 443
    if httpinstance.httpd_443_configured():
        sys.exit("Aborting installation")

    check_dirsrv()

    if not options.no_ntp:
        try:
            ipaclient.ntpconf.check_timedate_services()
        except ipaclient.ntpconf.NTPConflictingService, e:
            print("WARNING: conflicting time&date synchronization service '%s'"
                  " will" % e.conflicting_service)
            print("be disabled in favor of ntpd")
            print("")
        except ipaclient.ntpconf.NTPConfigurationError:
            pass

    api.bootstrap(context='installer')
    api.finalize()

    config = ReplicaConfig()
    config.realm_name = api.env.realm
    config.host_name = api.env.host
    config.domain_name = api.env.domain
    config.master_host_name = api.env.server
    config.ca_host_name = api.env.ca_host
    config.setup_ca = options.setup_ca
    config.setup_kra = options.setup_kra
    config.dir = installer._top_dir

    installutils.verify_fqdn(config.host_name, options.no_host_dns)
    installutils.verify_fqdn(config.master_host_name, options.no_host_dns)
    installutils.check_creds(options, config.realm_name)

    cafile = paths.IPA_CA_CRT
    if not ipautil.file_exists(cafile):
        raise RuntimeError("CA cert file is not available! Please reinstall"
                           "the client and try again.")

    ldapuri = 'ldaps://%s' % ipautil.format_netloc(config.master_host_name)
    remote_api = create_api(mode=None)
    remote_api.bootstrap(in_server=True, context='installer',
                         ldap_uri=ldapuri)
    remote_api.finalize()
    conn = remote_api.Backend.ldap2
    replman = None
    try:
        # Try out authentication
        conn.connect(ccache=os.environ.get('KRB5CCNAME'))
        replman = ReplicationManager(config.realm_name,
                                     config.master_host_name, None)

        # Check that we don't already have a replication agreement
        try:
            (acn, adn) = replman.agreement_dn(config.host_name)
            entry = conn.get_entry(adn, ['*'])
        except errors.NotFound:
            pass
        else:
            root_logger.info('Error: A replication agreement for this '
                             'host already exists.')
            print('A replication agreement for this host already exists. '
                  'It needs to be removed.')
            print("Run this command:")
            print("    %% ipa-replica-manage del %s --force" %
                  config.host_name)
            sys.exit(3)

        # Detect the current domain level
        try:
            current = remote_api.Command['domainlevel_get']()['result']
        except errors.NotFound:
            # If we're joining an older master, domain entry is not
            # available
            current = constants.DOMAIN_LEVEL_0

        if current == constants.DOMAIN_LEVEL_0:
            raise RuntimeError(
                "You must provide a file generated by ipa-replica-prepare to "
                "create a replica when the domain is at level 0."
            )

        # Detect if current level is out of supported range
        # for this IPA version
        under_lower_bound = current < constants.MIN_DOMAIN_LEVEL
        above_upper_bound = current > constants.MAX_DOMAIN_LEVEL

        if under_lower_bound or above_upper_bound:
            message = ("This version of FreeIPA does not support "
                       "the Domain Level which is currently set for "
                       "this domain. The Domain Level needs to be "
                       "raised before installing a replica with "
                       "this version is allowed to be installed "
                       "within this domain.")
            root_logger.error(message)
            sys.exit(3)

        # Detect if the other master can handle replication managers
        # cn=replication managers,cn=sysaccounts,cn=etc,$SUFFIX
        dn = DN(('cn', 'replication managers'), ('cn', 'sysaccounts'),
                ('cn', 'etc'), ipautil.realm_to_suffix(config.realm_name))
        try:
            entry = conn.get_entry(dn)
        except errors.NotFound:
            msg = ("The Replication Managers group is not available in "
                   "the domain. Replica promotion requires the use of "
                   "Replication Managers to be able to replicate data. "
                   "Upgrade the peer master or use the ipa-replica-prepare "
                   "command on the master and use a prep file to install "
                   "this replica.")
            root_logger.error(msg)
            sys.exit(3)

        dns_masters = remote_api.Object['dnsrecord'].get_dns_masters()
        if dns_masters:
            if not options.no_host_dns:
                root_logger.debug('Check forward/reverse DNS resolution')
                resolution_ok = (
                    check_dns_resolution(config.master_host_name,
                                         dns_masters) and
                    check_dns_resolution(config.host_name, dns_masters))
                if not resolution_ok and installer.interactive:
                    if not ipautil.user_input("Continue?", False):
                        sys.exit(0)
        else:
            root_logger.debug('No IPA DNS servers, '
                              'skipping forward/reverse resolution check')

        entry_attrs = conn.get_ipa_config()
        subject_base = entry_attrs.get('ipacertificatesubjectbase', [None])[0]
        if subject_base is not None:
            config.subject_base = DN(subject_base)

        # Find if any server has a CA
        ca_host = service.find_providing_server('CA', conn, api.env.server)
        if ca_host is not None:
            config.ca_host_name = ca_host
            ca_enabled = True
        else:
            # FIXME: add way to pass in certificates
            root_logger.error("The remote master does not have a CA "
                              "installed, can't proceed without certs")
            sys.exit(3)

        config.kra_host_name = service.find_providing_server('KRA', conn,
                                                             api.env.server)
        if options.setup_kra and config.kra_host_name is None:
            root_logger.error("There is no KRA server in the domain, can't "
                              "setup a KRA clone")
            sys.exit(3)

        if options.setup_ca:
            if not ca_enabled:
                root_logger.error("The remote master does not have a CA "
                                  "installed, can't set up CA")
                sys.exit(3)

            options.realm_name = config.realm_name
            options.host_name = config.host_name
            options.subject = config.subject_base
            ca.install_check(False, None, options)

        if config.setup_kra:
            try:
                kra.install_check(remote_api, config, options)
            except RuntimeError as e:
                print(str(e))
                sys.exit(1)
    except errors.ACIError:
        sys.exit("\nInsufficiently privileges to promote the server.")
    except errors.LDAPError:
        sys.exit("\nUnable to connect to LDAP server %s" %
                 config.master_host_name)
    finally:
        if replman and replman.conn:
            replman.conn.unbind()
        if conn.isconnected():
            conn.disconnect()

    if options.setup_dns:
        dns.install_check(False, True, options, config.host_name)
    else:
        config.ips = installutils.get_server_ip_address(
            config.host_name, not installer.interactive,
            False, options.ip_addresses)

    # check connection
    if not options.skip_conncheck:
        replica_conn_check(
            config.master_host_name, config.host_name, config.realm_name,
            options.setup_ca, 389,
            options.admin_password, principal=options.principal)

    if not ipautil.file_exists(cafile):
        raise RuntimeError("CA cert file is not available.")

    installer._ca_enabled = ca_enabled
    installer._fstore = fstore
    installer._sstore = sstore
    installer._config = config


@common_cleanup
def promote(installer):
    options = installer
    fstore = installer._fstore
    sstore = installer._sstore
    config = installer._config

    # Save client file and merge in server directives
    target_fname = paths.IPA_DEFAULT_CONF
    fstore.backup_file(target_fname)
    ipaconf = ipaclient.ipachangeconf.IPAChangeConf("IPA Replica Promote")
    ipaconf.setOptionAssignment(" = ")
    ipaconf.setSectionNameDelimiters(("[", "]"))

    config.promote = installer.promote
    config.dirman_password = hexlify(ipautil.ipa_generate_password())

    # FIXME: allow to use passed in certs instead
    if installer._ca_enabled:
        configure_certmonger()

    # Create DS user/group if it doesn't exist yet
    dsinstance.create_ds_user()

    # Configure ntpd
    if not options.no_ntp:
        ipaclient.ntpconf.force_ntpd(sstore)
        ntp = ntpinstance.NTPInstance()
        ntp.create_instance()

    try:
        # Configure dirsrv
        ds = install_replica_ds(config, options, promote=True)

        # Always try to install DNS records
        install_dns_records(config, options, api)

        # Must install http certs before changing ipa configuration file
        # or certmonger will fail to contact the peer master
        install_http_certs(config, fstore)

    finally:
        # Create the management framework config file
        # do this regardless of the state of DS installation. Even if it fails,
        # we need to have master-like configuration in order to perform a
        # successful uninstallation
        ldapi_uri = installutils.realm_to_ldapi_uri(config.realm_name)

        gopts = [
            ipaconf.setOption('host', config.host_name),
            ipaconf.rmOption('server'),
            ipaconf.setOption('xmlrpc_uri',
                              'https://%s/ipa/xml' %
                              ipautil.format_netloc(config.host_name)),
            ipaconf.setOption('ldap_uri', ldapi_uri),
            ipaconf.setOption('mode', 'production'),
            ipaconf.setOption('enable_ra', 'True'),
            ipaconf.setOption('ra_plugin', 'dogtag'),
            ipaconf.setOption('dogtag_version', '10')]
        opts = [ipaconf.setSection('global', gopts)]

        ipaconf.changeConf(target_fname, opts)
        os.chmod(target_fname, 0o644)   # must be readable for httpd

    custodia = custodiainstance.CustodiaInstance(config.host_name,
                                                 config.realm_name)
    custodia.create_replica(config.master_host_name)

    krb = install_krb(config,
                      setup_pkinit=not options.no_pkinit,
                      promote=True)

    http = install_http(config,
                        auto_redirect=not options.no_ui_redirect,
                        promote=True)

    # Apply any LDAP updates. Needs to be done after the replica is synced-up
    service.print_msg("Applying LDAP updates")
    ds.apply_updates()

    otpd = otpdinstance.OtpdInstance()
    otpd.create_instance('OTPD', config.host_name, config.dirman_password,
                         ipautil.realm_to_suffix(config.realm_name))

    if config.setup_ca:
        options.realm_name = config.realm_name
        options.domain_name = config.domain_name
        options.host_name = config.host_name
        options.dm_password = config.dirman_password
        ca_data = (os.path.join(config.dir, 'cacert.p12'),
                   config.dirman_password)
        custodia.get_ca_keys(config.ca_host_name, ca_data[0], ca_data[1])

        ca = cainstance.CAInstance(config.realm_name, certs.NSS_DIR,
                                   host_name=config.host_name,
                                   dm_password=config.dirman_password)
        ca.configure_replica(config.ca_host_name,
                             subject_base=config.subject_base,
                             ca_cert_bundle=ca_data)

    if options.setup_kra:
        ca_data = (os.path.join(config.dir, 'kracert.p12'),
                   config.dirman_password)
        custodia.get_kra_keys(config.kra_host_name, ca_data[0], ca_data[1])

        kra = krainstance.KRAInstance(config.realm_name)
        kra.configure_replica(config.host_name, config.kra_host_name,
                              config.dirman_password,
                              kra_cert_bundle=ca_data)


    ds.replica_populate()

    custodia.import_dm_password(config.master_host_name)

    promote_sssd(config.host_name)

    # Switch API so that it uses the new servr configuration
    server_api = create_api(mode=None)
    server_api.bootstrap(in_server=True, context='installer')
    server_api.finalize()

    if options.setup_dns:
        server_api.Backend.rpcclient.connect()
        server_api.Backend.ldap2.connect(autobind=True)
        dns.install(False, True, options, server_api)

    # Everything installed properly, activate ipa service.
    services.knownservices.ipa.enable()


class Replica(BaseServer):
    replica_file = Knob(
        str, None,
        description="a file generated by ipa-replica-prepare",
    )

    realm_name = None
    domain_name = None

    setup_ca = Knob(BaseServer.setup_ca)
    setup_kra = Knob(BaseServer.setup_kra)
    setup_dns = Knob(BaseServer.setup_dns)

    ip_addresses = Knob(
        BaseServer.ip_addresses,
        description=("Replica server IP Address. This option can be used "
                     "multiple times"),
    )

    dm_password = Knob(
        BaseServer.dm_password,
        description="Directory Manager (existing master) password",
        cli_name='password',
        cli_metavar='PASSWORD',
    )

    admin_password = Knob(
        BaseServer.admin_password,
        description="Admin user Kerberos password used for connection check",
        cli_short_name='w',
    )

    mkhomedir = Knob(BaseServer.mkhomedir)
    host_name = None
    no_host_dns = Knob(BaseServer.no_host_dns)
    no_ntp = Knob(BaseServer.no_ntp)
    no_pkinit = Knob(BaseServer.no_pkinit)
    no_ui_redirect = Knob(BaseServer.no_ui_redirect)
    ssh_trust_dns = Knob(BaseServer.ssh_trust_dns)
    no_ssh = Knob(BaseServer.no_ssh)
    no_sshd = Knob(BaseServer.no_sshd)
    no_dns_sshfp = Knob(BaseServer.no_dns_sshfp)

    skip_conncheck = Knob(
        bool, False,
        description="skip connection check to remote master",
    )

    principal = Knob(
        str, None,
        sensitive=True,
        description="User Principal allowed to promote replicas",
        cli_short_name='P',
    )

    promote = False

    # ca
    external_ca = None
    external_ca_type = None
    external_cert_files = None
    dirsrv_cert_files = None
    http_cert_files = None
    pkinit_cert_files = None
    dirsrv_pin = None
    http_pin = None
    pkinit_pin = None
    dirsrv_cert_name = None
    http_cert_name = None
    pkinit_cert_name = None
    ca_cert_files = None
    subject = None
    ca_signing_algorithm = None

    # dns
    dnssec_master = None
    disable_dnssec_master = None
    kasp_db_file = None
    force = None
    zonemgr = None

    def __init__(self, **kwargs):
        super(Replica, self).__init__(**kwargs)

        self._top_dir = None
        self._config = None
        self._update_hosts_file = False

        if self.replica_file is None:
            self.promote = True
        else:
            if not ipautil.file_exists(self.replica_file):
                raise RuntimeError("Replica file %s does not exist"
                                   % self.replica_file)

        if self.setup_dns:
            #pylint: disable=no-member
            if not self.dns.forwarders and not self.dns.no_forwarders:
                raise RuntimeError(
                    "You must specify at least one --forwarder option or "
                    "--no-forwarders option")

        self.password = self.dm_password

    @step()
    def main(self):
        if self.promote:
            promote_check(self)
            yield
            promote(self)
        else:
            with ipautil.private_ccache():
                install_check(self)
                yield
                install(self)