FROM alpine:latest
LABEL maintainer="wuzhij <wuzhij@qq.com>"

# 使用构建参数支持多架构构建
ARG TARGETARCH
ARG TARGETVARIANT
ENV VERSION=1.0.0
ENV TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /opt/wzfilemanager

# 1. 安装基础依赖：包含 gcompat 和 libc6-compat 确保二进制文件兼容
# 2. 安装基础工具：zip, tar, xz (用于解压官方7z的tar.xz包)
# 3. 安装 openssh-client 以支持 SFTP 子系统
RUN apk add --no-cache tzdata wget ca-certificates gcompat libc6-compat libstdc++ bash openssh-client zip tar xz \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone

# 多架构支持 - 下载主程序，并从官方下载 7z 和 rar 的预编译二进制文件
RUN case "${TARGETARCH}" in \
      "amd64") PLATFORM="amd64"; RAR_URL="https://www.rarlab.com/rar/rarlinux-x64-7.0.9.tar.gz"; SEVENZ_URL="https://github.com/ip7z/7zip/releases/download/24.08/7z2408-linux-x64.tar.xz" ;; \
      "arm64") PLATFORM="arm64"; RAR_URL="https://www.rarlab.com/rar/rarlinux-aarch64-7.0.9.tar.gz"; SEVENZ_URL="https://github.com/ip7z/7zip/releases/download/24.08/7z2408-linux-arm64.tar.xz" ;; \
      "arm") \
        case "${TARGETVARIANT}" in \
          "v7") PLATFORM="armv7"; RAR_URL="https://www.rarlab.com/rar/rarlinux-arm-7.0.9.tar.gz"; SEVENZ_URL="https://github.com/ip7z/7zip/releases/download/24.08/7z2408-linux-arm.tar.xz" ;; \
          *) PLATFORM="armv7"; RAR_URL="https://www.rarlab.com/rar/rarlinux-arm-7.0.9.tar.gz"; SEVENZ_URL="https://github.com/ip7z/7zip/releases/download/24.08/7z2408-linux-arm.tar.xz" ;; \
        esac ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}"; exit 1 ;; \
    esac \
    && echo "Building for platform: ${PLATFORM}" \
    # 1. 下载主程序二进制
    && wget --no-check-certificate -q -O /opt/wzfilemanager/wzfilemanager http://wuzhij.de/?/mv/wz/v${VERSION}/wzfilemanager-linux-${PLATFORM} \
    && chmod +x /opt/wzfilemanager/wzfilemanager \
    # 2. 下载并安装官方 RAR 和 UnRAR
    && wget -q -O /tmp/rar.tar.gz "$RAR_URL" \
    && tar -xzf /tmp/rar.tar.gz -C /tmp \
    && cp /tmp/rar/rar /usr/local/bin/ \
    && cp /tmp/rar/unrar /usr/local/bin/ \
    && chmod +x /usr/local/bin/rar /usr/local/bin/unrar \
    && rm -rf /tmp/rar* \
    # 3. 下载并安装官方 7-Zip (解压后重命名为 7z)
    && wget -q -O /tmp/7z.tar.xz "$SEVENZ_URL" \
    && mkdir -p /tmp/7z \
    && tar -xf /tmp/7z.tar.xz -C /tmp/7z \
    && cp /tmp/7z/7zzs /usr/local/bin/7z \
    && chmod +x /usr/local/bin/7z \
    && rm -rf /tmp/7z* \
    # 清理下载工具
    && apk del wget

# 复制启动脚本
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 36688
# 声明挂载点，config.json 和日志将存放在此
VOLUME ["/opt/wzfilemanager/data"]
CMD ["/start.sh"]
