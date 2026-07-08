FROM ubuntu:26.04 AS zapret-build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    git make gcc pkg-config ca-certificates \
    libnetfilter-queue-dev libnfnetlink-dev libmnl-dev zlib1g-dev libcap-dev \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth=1 https://github.com/bol-van/zapret /src/zapret \
 && make -C /src/zapret \
 && NFQWS="$(find /src/zapret -type f -name nfqws | head -n1)" \
 && install -m 755 "$NFQWS" /tmp/nfqws \
 && strip /tmp/nfqws || true


FROM ubuntu:26.04 AS rootfs-build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    gpg \
    dbus \
    iproute2 \
    iptables \
    ipset \
    socat \
    grep \
    busybox-static \
    libnetfilter-queue1 \
    libnfnetlink0 \
    libmnl0 \
    zlib1g \
    libcap2 \
 && mkdir -p /usr/share/keyrings \
 && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
    | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ resolute main" \
    > /etc/apt/sources.list.d/cloudflare-client.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends cloudflare-warp \
 && test -x /usr/bin/warp-svc \
 && test -x /usr/bin/warp-cli

COPY --from=zapret-build /tmp/nfqws /usr/local/bin/nfqws

RUN set -eux; \
    ROOT=/rootfs; \
    mkdir -p \
      "$ROOT" \
      "$ROOT/bin" \
      "$ROOT/dev" \
      "$ROOT/etc" \
      "$ROOT/home" \
      "$ROOT/lib" \
      "$ROOT/lib64" \
      "$ROOT/opt/zapret/files/fake" \
      "$ROOT/proc" \
      "$ROOT/root" \
      "$ROOT/run/dbus" \
      "$ROOT/sbin" \
      "$ROOT/sys" \
      "$ROOT/tmp" \
      "$ROOT/usr/bin" \
      "$ROOT/usr/lib" \
      "$ROOT/usr/lib/x86_64-linux-gnu" \
      "$ROOT/usr/local/bin" \
      "$ROOT/usr/sbin" \
      "$ROOT/usr/share" \
      "$ROOT/var/lib/cloudflare-warp" \
      "$ROOT/var/lib/dbus" \
      "$ROOT/var/log" \
      "$ROOT/var/tmp" \
      "$ROOT/config/lists"; \
    chmod 1777 "$ROOT/tmp" "$ROOT/var/tmp"; \
    \
    copy_file() { \
      src="$1"; \
      dst="$ROOT$src"; \
      mkdir -p "$(dirname "$dst")"; \
      cp -aL "$src" "$dst"; \
    }; \
    \
    copy_cmd() { \
      cmd="$1"; \
      p="$(command -v "$cmd")"; \
      real="$(readlink -f "$p")"; \
      copy_file "$real"; \
      if [ "$p" != "$real" ]; then \
        mkdir -p "$ROOT$(dirname "$p")"; \
        ln -sf "$real" "$ROOT$p"; \
      fi; \
    }; \
    \
    copy_ldd() { \
      bin="$1"; \
      ldd "$bin" \
        | awk '{ for (i=1; i<=NF; i++) if (index($i, "/") == 1) print $i }' \
        | sort -u \
        | while read -r lib; do \
            [ -n "$lib" ] && [ -e "$lib" ] && copy_file "$lib"; \
          done; \
    }; \
    \
    for interp in \
      /lib64/ld-linux-x86-64.so.2 \
      /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 \
      /usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 \
    ; do \
      [ -e "$interp" ] && copy_file "$interp"; \
    done; \
    \
    copy_cmd bash; \
    copy_cmd dbus-daemon; \
    copy_cmd dbus-uuidgen; \
    copy_cmd iptables; \
    copy_cmd ipset; \
    copy_cmd ss; \
    copy_cmd socat; \
    copy_cmd grep; \
    copy_cmd warp-svc; \
    copy_cmd warp-cli; \
    copy_file /usr/local/bin/nfqws; \
    \
    for b in \
      /usr/bin/bash \
      /usr/bin/dbus-daemon \
      /usr/bin/dbus-uuidgen \
      /usr/sbin/xtables-nft-multi \
      /usr/sbin/ipset \
      /usr/bin/ss \
      /usr/bin/socat1 \
      /usr/bin/grep \
      /usr/bin/warp-svc \
      /usr/bin/warp-cli \
      /usr/local/bin/nfqws \
    ; do \
      copy_ldd "$b"; \
    done; \
    \
    install -D -m 755 /bin/busybox "$ROOT/bin/busybox"; \
    ln -sf /usr/bin/bash "$ROOT/bin/bash"; \
    ln -sf /usr/bin/bash "$ROOT/bin/sh"; \
    for c in \
      tail seq sleep mkdir rm cat head true false test kill \
      ls du find sort awk readlink dirname cp chmod chown touch \
      echo printf env pwd basename wc tee tr cut date uname \
    ; do \
      ln -sf /bin/busybox "$ROOT/usr/bin/$c"; \
    done; \
    \
    cp -a /etc/passwd /etc/group /etc/nsswitch.conf "$ROOT/etc/"; \
    cp -a /etc/ssl "$ROOT/etc/"; \
    cp -a /usr/lib/ssl "$ROOT/usr/lib/" 2>/dev/null || true; \
    cp -a /etc/dbus-1 "$ROOT/etc/"; \
    cp -a /usr/share/dbus-1 "$ROOT/usr/share/"; \
    cp -a /usr/lib/dbus-1.0 "$ROOT/usr/lib/"; \
    cp -a /usr/share/ca-certificates "$ROOT/usr/share/"; \
    cp -a /usr/share/iptables "$ROOT/usr/share/" 2>/dev/null || true; \
    cp -a /usr/lib/x86_64-linux-gnu/xtables "$ROOT/usr/lib/x86_64-linux-gnu/"; \
    cp -a /usr/lib/x86_64-linux-gnu/libnss_*.so* "$ROOT/usr/lib/x86_64-linux-gnu/" 2>/dev/null || true; \
    \
    find /usr/lib/x86_64-linux-gnu/xtables -type f -name '*.so' -print \
      | while read -r x; do copy_ldd "$x"; done; \
    \
    dbus-uuidgen > "$ROOT/etc/machine-id"; \
    cp "$ROOT/etc/machine-id" "$ROOT/var/lib/dbus/machine-id"; \
    \
    echo "== rootfs size =="; \
    du -xhd1 "$ROOT" | sort -h; \
    echo "== biggest files =="; \
    find "$ROOT" -xdev -type f -size +2M -printf "%s %p\n" \
      | sort -n \
      | awk '{printf "%.1f MB  %s\n", $1/1024/1024, $2}'

COPY assets/fake /rootfs/opt/zapret/files/fake
COPY assets/lists /rootfs/config/lists
COPY config/zapret1-warp.conf /rootfs/config/zapret1-warp.conf
COPY entrypoint.sh /rootfs/entrypoint.sh

RUN set -eux; \
    sed -i '1s|^#!/usr/bin/env bash|#!/usr/bin/bash|' /rootfs/entrypoint.sh; \
    if ! grep -q 'rm -f /run/dbus/pid' /rootfs/entrypoint.sh; then \
      sed -i '/mkdir -p \/run\/dbus/a rm -f /run/dbus/pid' /rootfs/entrypoint.sh; \
    fi; \
    chmod +x /rootfs/entrypoint.sh; \
    chroot /rootfs /usr/bin/bash -lc ' \
      set -e; \
      for c in bash dbus-daemon dbus-uuidgen iptables ipset ss socat warp-svc warp-cli nfqws grep tail seq sleep mkdir rm; do \
        command -v "$c" >/dev/null || { echo "MISSING $c"; exit 1; }; \
      done; \
      warp-cli --version || true; \
      iptables --version; \
      dbus-daemon --version | head -1; \
      echo OK \
    '; \
    echo "== final rootfs size =="; \
    du -xhd1 /rootfs | sort -h; \
    du -xhd1 /rootfs/usr | sort -h; \
    echo "== final biggest files =="; \
    find /rootfs -xdev -type f -size +2M -printf "%s %p\n" \
      | sort -n \
      | awk '{printf "%.1f MB  %s\n", $1/1024/1024, $2}'


FROM scratch

LABEL warp_zapret1_slim="scratch-rootfs-no-ubuntu-base"

ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

COPY --from=rootfs-build /rootfs/ /

ENTRYPOINT ["/entrypoint.sh"]
