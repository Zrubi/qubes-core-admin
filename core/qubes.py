#!/usr/bin/python2
# -*- coding: utf-8 -*-
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2010  Joanna Rutkowska <joanna@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
#

import sys
import os
import os.path
import lxml.etree
import xml.parsers.expat
import fcntl
import time
import warnings
import tempfile
import grp

# Do not use XenAPI or create/read any VM files
# This is for testing only!
dry_run = False
#dry_run = True


if not dry_run:
    import xen.lowlevel.xc
    import xen.lowlevel.xl
    import xen.lowlevel.xs


qubes_base_dir   = "/var/lib/qubes"
system_path = {
    'qubes_guid_path': '/usr/bin/qubes-guid',
    'qrexec_daemon_path': '/usr/lib/qubes/qrexec-daemon',
    'qrexec_client_path': '/usr/lib/qubes/qrexec-client',

    'qubes_base_dir': qubes_base_dir,

    'qubes_appvms_dir': qubes_base_dir + '/appvms',
    'qubes_templates_dir': qubes_base_dir + '/vm-templates',
    'qubes_servicevms_dir': qubes_base_dir + '/servicevms',
    'qubes_store_filename': qubes_base_dir + '/qubes.xml',
    'qubes_kernels_base_dir': qubes_base_dir + '/vm-kernels',

    # qubes_icon_dir is obsolete
    # use QIcon.fromTheme() where applicable
    'qubes_icon_dir': '/usr/share/icons/hicolor/128x128/devices',

    'config_template_pv': '/usr/share/qubes/vm-template.conf',

    'qubes_pciback_cmd': '/usr/lib/qubes/unbind-pci-device.sh',
    'prepare_volatile_img_cmd': '/usr/lib/qubes/prepare-volatile-img.sh',
    'monitor_layout_notify_cmd': '/usr/bin/qubes-monitor-layout-notify',
}

vm_files = {
    'root_img': 'root.img',
    'rootcow_img': 'root-cow.img',
    'volatile_img': 'volatile.img',
    'clean_volatile_img': 'clean-volatile.img.tar',
    'private_img': 'private.img',
    'kernels_subdir': 'kernels',
    'firewall_conf': 'firewall.xml',
    'whitelisted_appmenus': 'whitelisted-appmenus.list',
    'updates_stat_file': 'updates.stat',
}

defaults = {
    'memory': 400,
    'kernelopts': "nopat",
    'kernelopts_pcidevs': "nopat iommu=soft swiotlb=4096",

    'dom0_update_check_interval': 6*3600,

    # how long (in sec) to wait for VMs to shutdown,
    # before killing them (when used qvm-run with --wait option),
    'shutdown_counter_max': 60,

    'vm_default_netmask': "255.255.255.0",

    # Set later
    'appvm_label': None,
    'template_label': None,
    'servicevm_label': None,
}

qubes_max_qid = 254
qubes_max_netid = 254

class QubesException (Exception):
    pass

if not dry_run:
    xc = xen.lowlevel.xc.xc()
    xs = xen.lowlevel.xs.xs()
    xl_ctx = xen.lowlevel.xl.ctx()

class QubesHost(object):
    def __init__(self):
        self.physinfo = xc.physinfo()

        self.xen_total_mem = long(self.physinfo['total_memory'])
        self.xen_no_cpus = self.physinfo['nr_cpus']

#        print "QubesHost: total_mem  = {0}B".format (self.xen_total_mem)
#        print "QubesHost: free_mem   = {0}".format (self.get_free_xen_memory())
#        print "QubesHost: total_cpus = {0}".format (self.xen_no_cpus)

    @property
    def memory_total(self):
        return self.xen_total_mem

    @property
    def no_cpus(self):
        return self.xen_no_cpus

    def get_free_xen_memory(self):
        ret = self.physinfo['free_memory']
        return long(ret)

    def measure_cpu_usage(self, previous=None, previous_time = None,
            wait_time=1):
        """measure cpu usage for all domains at once"""
        if previous is None:
            previous_time = time.time()
            previous = {}
            info = xc.domain_getinfo(0, qubes_max_qid)
            for vm in info:
                previous[vm['domid']] = {}
                previous[vm['domid']]['cpu_time'] = (
                        vm['cpu_time'] / vm['online_vcpus'])
                previous[vm['domid']]['cpu_usage'] = 0
            time.sleep(wait_time)

        current_time = time.time()
        current = {}
        info = xc.domain_getinfo(0, qubes_max_qid)
        for vm in info:
            current[vm['domid']] = {}
            current[vm['domid']]['cpu_time'] = (
                    vm['cpu_time'] / max(vm['online_vcpus'], 1))
            if vm['domid'] in previous.keys():
                current[vm['domid']]['cpu_usage'] = (
                    float(current[vm['domid']]['cpu_time'] -
                        previous[vm['domid']]['cpu_time']) /
                    long(1000**3) / (current_time-previous_time) * 100)
                if current[vm['domid']]['cpu_usage'] < 0:
                    # VM has been rebooted
                    current[vm['domid']]['cpu_usage'] = 0
            else:
                current[vm['domid']]['cpu_usage'] = 0

        return (current_time, current)

class QubesVmLabel(object):
    def __init__(self, index, color, name, dispvm=False):
        self.index = index
        self.color = color
        self.name = name
        self.dispvm = dispvm

        self.icon = '{}-{}'.format(('dispvm' if dispvm else 'appvm'), name)

        # self.icon_path is obsolete
        # use QIcon.fromTheme(label.icon) where applicable
        self.icon_path = os.path.join(
                system_path['qubes_icon_dir'], self.icon) + ".png"

    def __repr__(self):
        return '{}({!r}, {!r}, {!r}, dispvm={!r})'.format(
            self.__class__.__name__,
            self.index,
            self.color,
            self.name,
            self.dispvm)

# Globally defined lables
QubesVmLabels = {
    "red":      QubesVmLabel(1, "0xcc0000", "red"     ),
    "orange":   QubesVmLabel(2, "0xf57900", "orange"  ),
    "yellow":   QubesVmLabel(3, "0xedd400", "yellow"  ),
    "green":    QubesVmLabel(4, "0x73d216", "green"   ),
    "gray":     QubesVmLabel(5, "0x555753", "gray"    ),
    "blue":     QubesVmLabel(6, "0x3465a4", "blue"    ),
    "purple":   QubesVmLabel(7, "0x75507b", "purple"  ),
    "black":    QubesVmLabel(8, "0x000000", "black"   ),
}

QubesDispVmLabels = {
    "red":      QubesVmLabel(1, "0xcc0000", "red",      dispvm=True),
    "orange":   QubesVmLabel(2, "0xf57900", "orange",   dispvm=True),
    "yellow":   QubesVmLabel(3, "0xedd400", "yellow",   dispvm=True),
    "green":    QubesVmLabel(4, "0x73d216", "green",    dispvm=True),
    "gray":     QubesVmLabel(5, "0x555753", "gray",     dispvm=True),
    "blue":     QubesVmLabel(6, "0x3465a4", "blue",     dispvm=True),
    "purple":   QubesVmLabel(7, "0x75507b", "purple",   dispvm=True),
    "black":    QubesVmLabel(8, "0x000000", "black",    dispvm=True),
}

defaults["appvm_label"] = QubesVmLabels["red"]
defaults["template_label"] = QubesVmLabels["black"]
defaults["servicevm_label"] = QubesVmLabels["red"]

QubesVmClasses = {}
def register_qubes_vm_class(vm_class):
    QubesVmClasses[vm_class.__name__] = vm_class
    # register class as local for this module - to make it easy to import from
    # other modules
    setattr(sys.modules[__name__], vm_class.__name__, vm_class)

class QubesVmCollection(dict):
    """
    A collection of Qubes VMs indexed by Qubes id (qid)
    """

    def __init__(self, store_filename=system_path["qubes_store_filename"]):
        super(QubesVmCollection, self).__init__()
        self.default_netvm_qid = None
        self.default_fw_netvm_qid = None
        self.default_template_qid = None
        self.default_kernel = None
        self.updatevm_qid = None
        self.qubes_store_filename = store_filename
        self.clockvm_qid = None
        self.qubes_store_file = None

    def __repr__(self):
        return '<{} {!r}>'.format(self.__class__.__name__, list(sorted(self.keys())))

    def values(self):
        for qid in self.keys():
            yield self[qid]

    def items(self):
        for qid in self.keys():
            yield (qid, self[qid])

    def __iter__(self):
        for qid in sorted(super(QubesVmCollection, self).keys()):
            yield qid

    keys = __iter__

    def __setitem__(self, key, value):
        if key not in self:
            return super(QubesVmCollection, self).__setitem__(key, value)
        else:
            assert False, "Attempt to add VM with qid that already exists in the collection!"

    def add_new_vm(self, vm_type, **kwargs):
        if vm_type not in QubesVmClasses.keys():
            raise ValueError("Unknown VM type: %s" % vm_type)

        qid = self.get_new_unused_qid()
        vm_cls = QubesVmClasses[vm_type]
        if 'template' in kwargs:
            if not vm_cls.is_template_compatible(kwargs['template']):
                raise QubesException("Template not compatible with selected "
                                     "VM type")

        vm = vm_cls(qid=qid, collection=self, **kwargs)
        if not self.verify_new_vm(vm):
            raise QubesException("Wrong VM description!")
        self[vm.qid] = vm

        # make first created NetVM the default one
        if self.default_fw_netvm_qid is None and vm.is_netvm():
            self.set_default_fw_netvm(vm)

        if self.default_netvm_qid is None and vm.is_proxyvm():
            self.set_default_netvm(vm)

        # make first created TemplateVM the default one
        if self.default_template_qid is None and vm.is_template():
            self.set_default_template(vm)

        # make first created ProxyVM the UpdateVM
        if self.updatevm_qid is None and vm.is_proxyvm():
            self.set_updatevm_vm(vm)

        # by default ClockVM is the first NetVM
        if self.clockvm_qid is None and vm.is_netvm():
            self.set_clockvm_vm(vm)

        return vm

    def add_new_appvm(self, name, template,
                      dir_path = None, conf_file = None,
                      private_img = None,
                      label = None):

        warnings.warn("Call to deprecated function, use add_new_vm instead",
                DeprecationWarning, stacklevel=2)
        return self.add_new_vm("QubesAppVm", name=name, template=template,
                         dir_path=dir_path, conf_file=conf_file,
                         private_img=private_img,
                         netvm = self.get_default_netvm(),
                         kernel = self.get_default_kernel(),
                         uses_default_kernel = True,
                         label=label)

    def add_new_hvm(self, name, label = None):

        warnings.warn("Call to deprecated function, use add_new_vm instead",
                DeprecationWarning, stacklevel=2)
        return self.add_new_vm("QubesHVm", name=name, label=label)

    def add_new_disposablevm(self, name, template, dispid,
                      label = None, netvm = None):

        warnings.warn("Call to deprecated function, use add_new_vm instead",
                DeprecationWarning, stacklevel=2)
        return self.add_new_vm("QubesDisposableVm", name=name, template=template,
                         netvm = netvm,
                         label=label, dispid=dispid)

    def add_new_templatevm(self, name,
                           dir_path = None, conf_file = None,
                           root_img = None, private_img = None,
                           installed_by_rpm = True):

        warnings.warn("Call to deprecated function, use add_new_vm instead",
                DeprecationWarning, stacklevel=2)
        return self.add_new_vm("QubesTemplateVm", name=name,
                              dir_path=dir_path, conf_file=conf_file,
                              root_img=root_img, private_img=private_img,
                              installed_by_rpm=installed_by_rpm,
                              netvm = self.get_default_netvm(),
                              kernel = self.get_default_kernel(),
                              uses_default_kernel = True)

    def add_new_netvm(self, name, template,
                      dir_path = None, conf_file = None,
                      private_img = None, installed_by_rpm = False,
                      label = None):

        warnings.warn("Call to deprecated function, use add_new_vm instead",
                DeprecationWarning, stacklevel=2)
        return self.add_new_vm("QubesNetVm", name=name, template=template,
                         label=label,
                         private_img=private_img, installed_by_rpm=installed_by_rpm,
                         uses_default_kernel = True,
                         dir_path=dir_path, conf_file=conf_file)

    def add_new_proxyvm(self, name, template,
                     dir_path = None, conf_file = None,
                     private_img = None, installed_by_rpm = False,
                     label = None):

        warnings.warn("Call to deprecated function, use add_new_vm instead",
                DeprecationWarning, stacklevel=2)
        return self.add_new_vm("QubesProxyVm", name=name, template=template,
                              label=label,
                              private_img=private_img, installed_by_rpm=installed_by_rpm,
                              dir_path=dir_path, conf_file=conf_file,
                              uses_default_kernel = True,
                              netvm = self.get_default_fw_netvm())

    def set_default_template(self, vm):
        if vm is None:
            self.default_template_qid = None
        else:
            assert vm.is_template(), "VM {0} is not a TemplateVM!".format(vm.name)
            self.default_template_qid = vm.qid

    def get_default_template(self):
        if self.default_template_qid is None:
            return None
        else:
            return self[self.default_template_qid]

    def set_default_netvm(self, vm):
        if vm is None:
            self.default_netvm_qid = None
        else:
            assert vm.is_netvm(), "VM {0} does not provide network!".format(vm.name)
            self.default_netvm_qid = vm.qid

    def get_default_netvm(self):
        if self.default_netvm_qid is None:
            return None
        else:
            return self[self.default_netvm_qid]

    def set_default_kernel(self, kernel):
        assert os.path.exists(
                os.path.join(system_path["qubes_kernels_base_dir"], kernel)), \
            "Kerel {0} not installed!".format(kernel)
        self.default_kernel = kernel

    def get_default_kernel(self):
        return self.default_kernel

    def set_default_fw_netvm(self, vm):
        if vm is None:
            self.default_fw_netvm_qid = None
        else:
            assert vm.is_netvm(), "VM {0} does not provide network!".format(vm.name)
            self.default_fw_netvm_qid = vm.qid

    def get_default_fw_netvm(self):
        if self.default_fw_netvm_qid is None:
            return None
        else:
            return self[self.default_fw_netvm_qid]

    def set_updatevm_vm(self, vm):
        if vm is None:
            self.updatevm_qid = None
        else:
            self.updatevm_qid = vm.qid

    def get_updatevm_vm(self):
        if self.updatevm_qid is None:
            return None
        else:
            return self[self.updatevm_qid]

    def set_clockvm_vm(self, vm):
        if vm is None:
            self.clockvm_qid = None
        else:
            self.clockvm_qid = vm.qid

    def get_clockvm_vm(self):
        if self.clockvm_qid is None:
            return None
        else:
            return self[self.clockvm_qid]

    def get_vm_by_name(self, name):
        for vm in self.values():
            if (vm.name == name):
                return vm
        return None

    def get_qid_by_name(self, name):
        vm = self.get_vm_by_name(name)
        return vm.qid if vm is not None else None

    def get_vms_based_on(self, template_qid):
        vms = set([vm for vm in self.values()
                   if (vm.template and vm.template.qid == template_qid)])
        return vms

    def get_vms_connected_to(self, netvm_qid):
        new_vms = [ netvm_qid ]
        dependend_vms_qid = []

        # Dependency resolving only makes sense on NetVM (or derivative)
        if not self[netvm_qid].is_netvm():
            return set([])

        while len(new_vms) > 0:
            cur_vm = new_vms.pop()
            for vm in self[cur_vm].connected_vms.values():
                if vm.qid not in dependend_vms_qid:
                    dependend_vms_qid.append(vm.qid)
                    if vm.is_netvm():
                        new_vms.append(vm.qid)

        vms = [vm for vm in self.values() if vm.qid in dependend_vms_qid]
        return vms

    def verify_new_vm(self, new_vm):

        # Verify that qid is unique
        for vm in self.values():
            if vm.qid == new_vm.qid:
                print >> sys.stderr, "ERROR: The qid={0} is already used by VM '{1}'!".\
                        format(vm.qid, vm.name)
                return False

        # Verify that name is unique
        for vm in self.values():
            if vm.name == new_vm.name:
                print >> sys.stderr, \
                    "ERROR: The name={0} is already used by other VM with qid='{1}'!".\
                        format(vm.name, vm.qid)
                return False

        return True

    def get_new_unused_qid(self):
        used_ids = set([vm.qid for vm in self.values()])
        for id in range (1, qubes_max_qid):
            if id not in used_ids:
                return id
        raise LookupError ("Cannot find unused qid!")

    def get_new_unused_netid(self):
        used_ids = set([vm.netid for vm in self.values() if vm.is_netvm()])
        for id in range (1, qubes_max_netid):
            if id not in used_ids:
                return id
        raise LookupError ("Cannot find unused netid!")


    def check_if_storage_exists(self):
        try:
            f = open (self.qubes_store_filename, 'r')
        except IOError:
            return False
        f.close()
        return True

    def create_empty_storage(self):
        self.qubes_store_file = open (self.qubes_store_filename, 'w')
        self.clear()
        self.save()

    def lock_db_for_reading(self):
        # save() would rename the file over qubes.xml, _then_ release lock,
        # so we need to ensure that the file for which we've got the lock is
        # still the right file
        while True:
            self.qubes_store_file = open (self.qubes_store_filename, 'r')
            fcntl.lockf(self.qubes_store_file, fcntl.LOCK_SH)
            if os.fstat(self.qubes_store_file.fileno()) == os.stat(
                    self.qubes_store_filename):
                break
            self.qubes_store_file.close()

    def lock_db_for_writing(self):
        # save() would rename the file over qubes.xml, _then_ release lock,
        # so we need to ensure that the file for which we've got the lock is
        # still the right file
        while True:
            self.qubes_store_file = open (self.qubes_store_filename, 'r+')
            fcntl.lockf(self.qubes_store_file, fcntl.LOCK_EX)
            if os.fstat(self.qubes_store_file.fileno()) == os.stat(
                    self.qubes_store_filename):
                break
            self.qubes_store_file.close()

    def unlock_db(self):
        # intentionally do not call explicit unlock to not unlock the file
        # before all buffers are flushed
        self.qubes_store_file.close()

    def save(self):
        root = lxml.etree.Element(
            "QubesVmCollection",

            default_template=str(self.default_template_qid) \
            if self.default_template_qid is not None else "None",

            default_netvm=str(self.default_netvm_qid) \
            if self.default_netvm_qid is not None else "None",

            default_fw_netvm=str(self.default_fw_netvm_qid) \
            if self.default_fw_netvm_qid is not None else "None",

            updatevm=str(self.updatevm_qid) \
            if self.updatevm_qid is not None else "None",

            clockvm=str(self.clockvm_qid) \
            if self.clockvm_qid is not None else "None",

            default_kernel=str(self.default_kernel) \
            if self.default_kernel is not None else "None",
        )

        for vm in self.values():
            element = vm.create_xml_element()
            if element is not None:
                root.append(element)
        tree = lxml.etree.ElementTree(root)

        try:

            new_store_file = tempfile.NamedTemporaryFile(prefix=self.qubes_store_filename, delete=False)
            # XXX: do not get lock on the new file, as in all use cases
            # unlock_db() is the next operation after save()
            tree.write(new_store_file, encoding="UTF-8", pretty_print=True)
            new_store_file.flush()
            os.chmod(new_store_file.name, 0660)
            os.chown(new_store_file.name, -1, grp.getgrnam('qubes').gr_gid)
            os.rename(new_store_file.name, self.qubes_store_filename)
            self.qubes_store_file.close()
            self.qubes_store_file = new_store_file
        except EnvironmentError as err:
            print("{0}: export error: {1}".format(
                os.path.basename(sys.argv[0]), err))
            return False
        return True

    def set_netvm_dependency(self, element):
        kwargs = {}
        attr_list = ("qid", "uses_default_netvm", "netvm_qid")

        for attribute in attr_list:
            kwargs[attribute] = element.get(attribute)

        vm = self[int(kwargs["qid"])]

        if "uses_default_netvm" not in kwargs:
            vm.uses_default_netvm = True
        else:
            vm.uses_default_netvm = (
                    True if kwargs["uses_default_netvm"] == "True" else False)
        if vm.uses_default_netvm is True:
            if vm.is_proxyvm():
                netvm = self.get_default_fw_netvm()
            else:
                netvm = self.get_default_netvm()
            kwargs.pop("netvm_qid")
        else:
            if kwargs["netvm_qid"] == "none" or kwargs["netvm_qid"] is None:
                netvm = None
                kwargs.pop("netvm_qid")
            else:
                netvm_qid = int(kwargs.pop("netvm_qid"))
                if netvm_qid not in self:
                    netvm = None
                else:
                    netvm = self[netvm_qid]

        # directly set internal attr to not call setters...
        vm._netvm = netvm
        if netvm:
            netvm.connected_vms[vm.qid] = vm


    def load_globals(self, element):
        default_template = element.get("default_template")
        self.default_template_qid = int(default_template) \
                if default_template.lower() != "none" else None

        default_netvm = element.get("default_netvm")
        if default_netvm is not None:
            self.default_netvm_qid = int(default_netvm) \
                    if default_netvm != "None" else None
            #assert self.default_netvm_qid is not None

        default_fw_netvm = element.get("default_fw_netvm")
        if default_fw_netvm is not None:
            self.default_fw_netvm_qid = int(default_fw_netvm) \
                    if default_fw_netvm != "None" else None
            #assert self.default_netvm_qid is not None

        updatevm = element.get("updatevm")
        if updatevm is not None:
            self.updatevm_qid = int(updatevm) \
                    if updatevm != "None" else None
            #assert self.default_netvm_qid is not None

        clockvm = element.get("clockvm")
        if clockvm is not None:
            self.clockvm_qid = int(clockvm) \
                    if clockvm != "None" else None

        self.default_kernel = element.get("default_kernel")


    def load(self):
        self.clear()

        try:
            tree = lxml.etree.parse(self.qubes_store_file)
        except (EnvironmentError,
                xml.parsers.expat.ExpatError) as err:
            print("{0}: import error: {1}".format(
                os.path.basename(sys.argv[0]), err))
            return False

        self.load_globals(tree.getroot())

        for (vm_class_name, vm_class) in sorted(QubesVmClasses.items(),
                key=lambda _x: _x[1].load_order):
            vms_of_class = tree.findall(vm_class_name)
            # first non-template based, then template based
            sorted_vms_of_class = sorted(vms_of_class, key= \
                    lambda x: str(x.get('template_qid')).lower() != "none")
            for element in sorted_vms_of_class:
                try:
                    vm = vm_class(xml_element=element, collection=self)
                    self[vm.qid] = vm
                except (ValueError, LookupError) as err:
                    print("{0}: import error ({1}): {2}".format(
                        os.path.basename(sys.argv[0]), vm_class_name, err))
                    raise
                    return False

        # After importing all VMs, set netvm references, in the same order
        for (vm_class_name, vm_class) in sorted(QubesVmClasses.items(),
                key=lambda _x: _x[1].load_order):
            for element in tree.findall(vm_class_name):
                try:
                    self.set_netvm_dependency(element)
                except (ValueError, LookupError) as err:
                    print("{0}: import error2 ({}): {}".format(
                        os.path.basename(sys.argv[0]), vm_class_name, err))
                    return False

        # if there was no clockvm entry in qubes.xml, try to determine default:
        # root of default NetVM chain
        if tree.getroot().get("clockvm") is None:
            if self.default_netvm_qid is not None:
                clockvm = self[self.default_netvm_qid]
                # Find root of netvm chain
                while clockvm.netvm is not None:
                    clockvm = clockvm.netvm

                self.clockvm_qid = clockvm.qid

        # Disable ntpd in ClockVM - to not conflict with ntpdate (both are
        # using 123/udp port)
        if self.clockvm_qid is not None:
            self[self.clockvm_qid].services['ntpd'] = False

        # Add dom0 if wasn't present in qubes.xml
        if not 0 in self.keys():
            dom0vm = QubesAdminVm (collection=self)
            self[dom0vm.qid] = dom0vm

        return True

    def pop(self, qid):
        if self.default_netvm_qid == qid:
            self.default_netvm_qid = None
        if self.default_fw_netvm_qid == qid:
            self.default_fw_netvm_qid = None
        if self.clockvm_qid == qid:
            self.clockvm_qid = None
        if self.updatevm_qid == qid:
            self.updatevm_qid = None
        if self.default_template_qid == qid:
            self.default_template_qid = None

        return super(QubesVmCollection, self).pop(qid)

class QubesDaemonPidfile(object):
    def __init__(self, name):
        self.name = name
        self.path = "/var/run/qubes/" + name + ".pid"

    def create_pidfile(self):
        f = open (self.path, 'w')
        f.write(str(os.getpid()))
        f.close()

    def pidfile_exists(self):
        return os.path.exists(self.path)

    def read_pid(self):
        f = open (self.path)
        pid = f.read ().strip()
        f.close()
        return int(pid)

    def pidfile_is_stale(self):
        if not self.pidfile_exists():
            return False

        # check if the pid file is valid...
        proc_path = "/proc/" + str(self.read_pid()) + "/cmdline"
        if not os.path.exists (proc_path):
            print >> sys.stderr, \
                "Path {0} doesn't exist, assuming stale pidfile.".\
                    format(proc_path)
            return True

        return False # It's a good pidfile

    def remove_pidfile(self):
        os.remove (self.path)

    def __enter__ (self):
        # assumes the pidfile doesn't exist -- you should ensure it before opening the context
        self.create_pidfile()

    def __exit__ (self, exc_type, exc_val, exc_tb):
        self.remove_pidfile()
        return False

modules_dir = os.path.join(os.path.dirname(__file__), 'modules')
for module_file in sorted(os.listdir(modules_dir)):
    if not module_file.endswith(".py") or module_file == "__init__.py":
        continue
    __import__('qubes.modules.%s' % module_file[:-3])

# vim:sw=4:et:
