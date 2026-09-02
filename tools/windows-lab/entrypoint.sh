#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

die() {
    printf '%s\n' "windows-lab: $*" >&2
    exit 1
}

first_file() {
    for candidate in "$@"; do
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
    die "/dev/kvm is unavailable"
fi
test -r /iso/windows.iso || die "the configured Windows ISO is unavailable"
test -f /state/disk/run.qcow2 || die "run labctl init first"
if [ "${LAB_ATTACH_UNATTEND:-1}" = 1 ]; then
    test -r /state/unattend.iso || die "the generated unattended ISO is unavailable"
fi

code_file="$(first_file \
    /usr/share/OVMF/OVMF_CODE_4M.ms.fd \
    /usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
    /usr/share/OVMF/OVMF_CODE_4M.fd)" || die "4 MiB OVMF code image not found"
vars_template="$(first_file \
    /usr/share/OVMF/OVMF_VARS_4M.ms.fd \
    /usr/share/OVMF/OVMF_VARS_4M.fd)" || die "4 MiB OVMF variable image not found"

mkdir -p /state/run/tpm /run/qemu
if [ ! -f /state/run/nvram.fd ]; then
    cp "$vars_template" /state/run/nvram.fd
fi

rm -f /run/qemu/swtpm.sock /run/qemu/hmp.sock
swtpm socket \
    --tpm2 \
    --tpmstate dir=/state/run/tpm \
    --ctrl type=unixio,path=/run/qemu/swtpm.sock \
    --flags startup-clear \
    --daemon

websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 &

boot_order=order=c,menu=on
if [ "${LAB_ATTACH_UNATTEND:-1}" = 1 ]; then
    boot_order=order=c,once=d,menu=on
fi

set -- \
    qemu-system-x86_64 \
    -name specbutler-windows-11 \
    -enable-kvm \
    -machine q35,accel=kvm,smm=on \
    -cpu host \
    -smp "${LAB_CPUS:-8}" \
    -m "${LAB_MEMORY:-16G}" \
    -global driver=cfi.pflash01,property=secure,value=on \
    -drive if=pflash,format=raw,readonly=on,file="$code_file" \
    -drive if=pflash,format=raw,file=/state/run/nvram.fd \
    -chardev socket,id=chrtpm,path=/run/qemu/swtpm.sock \
    -tpmdev emulator,id=tpm0,chardev=chrtpm \
    -device tpm-crb,tpmdev=tpm0 \
    -drive file=/state/disk/run.qcow2,if=none,id=osdisk,format=qcow2,cache=writeback,discard=unmap \
    -device nvme,drive=osdisk,serial="${LAB_DISK_SERIAL:?missing disk serial}" \
    -netdev user,id=net0,hostfwd=tcp:0.0.0.0:2222-:22,hostfwd=tcp:0.0.0.0:3389-:3389 \
    -device e1000e,netdev=net0,mac="${LAB_VM_MAC:?missing VM MAC}" \
    -device qemu-xhci \
    -device usb-kbd \
    -device usb-tablet \
    -uuid "${LAB_VM_UUID:?missing VM UUID}" \
    -rtc base=localtime,clock=host \
    -boot "$boot_order" \
    -display none \
    -vnc 127.0.0.1:0 \
    -monitor unix:/run/qemu/hmp.sock,server=on,wait=off \
    -pidfile /state/run/qemu.pid

if [ "${LAB_ATTACH_UNATTEND:-1}" = 1 ]; then
    set -- "$@" \
        -drive file=/iso/windows.iso,media=cdrom,readonly=on \
        -drive file=/state/unattend.iso,media=cdrom,readonly=on

    # Windows installation media prompts for a key before the unattended file
    # takes over. Send a bounded sequence only during installation boots.
    (
        attempt=0
        while [ "$attempt" -lt 15 ]; do
            sleep 1
            if [ -S /run/qemu/hmp.sock ]; then
                printf 'sendkey spc\n' | socat - UNIX-CONNECT:/run/qemu/hmp.sock >/dev/null 2>&1 || true
            fi
            attempt=$((attempt + 1))
        done
    ) &
fi

exec "$@"
