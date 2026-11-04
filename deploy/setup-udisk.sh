#!/usr/bin/env bash
# 在 Manager-Node 上：测速 sdb(UDisk) vs sda(系统盘)，sdb 更快则格式化为 ext4 挂到 /mnt/udisk，
# 否则恢复 swap 原样。会清空 /dev/sdb（已只读扫描确认无用户数据，仅 swap 残片）。
#
# 从 Windows Git Bash 仓库根目录执行：
#   ssh Manager-Node 'sudo bash -s' < deploy/setup-udisk.sh
set -euo pipefail
DEV=/dev/sdb
MNT=/mnt/udisk

# ---- 安全检查：必须是 UDisk、不能是根盘、不能已挂载
model=$(cat /sys/block/sdb/device/model | xargs)
[[ "$model" == "UDisk" ]] || { echo "sdb 型号是 '$model' 不是 UDisk，中止"; exit 1; }
root_dev=$(findmnt -no SOURCE / )
[[ "$root_dev" != $DEV* ]] || { echo "sdb 是根盘，中止"; exit 1; }
if findmnt -rno SOURCE | grep -q "^$DEV"; then echo "sdb 已挂载，中止"; exit 1; fi
SWAP_UUID=$(blkid -s UUID -o value $DEV || true)

echo ">>> 关闭 sdb 上的 swap"
grep -q "^$DEV " /proc/swaps && swapoff $DEV
tail -n +2 /proc/swaps

secs() { # 从 dd 输出里取耗时秒数，超时记 40
  local out; out=$(timeout 40 "$@" 2>&1 | tail -1) || true
  echo "$out" >&2
  echo "$out" | grep -oE '[0-9.]+ s,' | grep -oE '[0-9.]+' || echo 40
}
echo ">>> 测速（4K 同步写 = 小文件/uv/日志场景，决定性指标）"
echo "sdb 顺序写 64MB:" >&2; secs dd if=/dev/zero of=$DEV bs=1M count=64 seek=2048 oflag=direct >/dev/null
echo "sdb 4K 同步写 200 次:" >&2; B=$(secs dd if=/dev/zero of=$DEV bs=4k count=200 seek=600000 oflag=direct,dsync)
echo "sda 顺序写 16MB:" >&2; secs dd if=/dev/zero of=/root/ddt bs=1M count=16 oflag=direct >/dev/null; rm -f /root/ddt
echo "sda 4K 同步写 200 次:" >&2; A=$(secs dd if=/dev/zero of=/root/ddt bs=4k count=200 oflag=direct,dsync); rm -f /root/ddt
echo ">>> 4K 同步写 200 次耗时：sdb=${B}s  sda=${A}s"

if ! awk -v a="$A" -v b="$B" 'BEGIN{exit !(b < a)}'; then
  echo ">>> sdb 不比 sda 快，恢复 swap，不做格式化"
  mkswap ${SWAP_UUID:+-U $SWAP_UUID} $DEV >/dev/null && swapon -p 10 $DEV
  tail -n +2 /proc/swaps
  exit 0
fi

echo ">>> sdb 更快：格式化为 ext4 并挂载到 $MNT"
cp /etc/fstab /etc/fstab.bak.$(date +%Y%m%d-%H%M%S)
wipefs -aq $DEV
echo 'type=83' | sfdisk -q --label dos $DEV
partprobe $DEV 2>/dev/null || true; udevadm settle
for i in $(seq 20); do [ -b ${DEV}1 ] && break; sleep 0.5; done
mkfs.ext4 -q -F -L udisk ${DEV}1
UUID=$(blkid -s UUID -o value ${DEV}1)
sed -i "\|^$DEV[[:space:]].*swap|d" /etc/fstab          # 去掉旧 swap 行
grep -q "$MNT" /etc/fstab || echo "UUID=$UUID $MNT ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2" >> /etc/fstab
mkdir -p $MNT
systemctl daemon-reload
mount $MNT
chown pi:pi $MNT
echo ">>> 完成"
findmnt $MNT
df -h $MNT
grep -v '^#' /etc/fstab
