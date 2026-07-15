FROM alpine:3.20
LABEL maintainer="wuzhij <wuzhij@qq.com>"

# 使用构建参数支持多架构构建
ARG TARGETARCH
ARG TARGETVARIANT
ENV VERSION=1.0.0
ENV TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /opt/wzfilemanager

# 1. 安装基础依赖：包含 gcompat 和 libc6-compat 确保二进制文件兼容
# 2. 安装压缩工具：zip, tar, 7zip (提供 7zz 命令)
# 3. 安装 openssh-client 以支持 SFTP 子系统
RUN apk add --no-cache tzdata wget ca-certificates gcompat libc6-compat libstdc++ bash openssh-client zip tar 7zip \
    && ln -s /usr/bin/7zz /usr/bin/7z \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone

# 多架构支持 - 下载程序主体并从官方下载全架构 rar
RUN case "${TARGETARCH}" in \
      "amd64") PLATFORM="amd64"; RAR_URL="https://www.rarlab.com/rar/rarlinux-x64-7.0.9.tar.gz" ;; \
      "arm64") PLATFORM="arm64"; RAR_URL="https://www.rarlab.com/rar/rarlinux-aarch64-7.0.9.tar.gz" ;; \
      "arm") \
        case "${TARGETVARIANT}" in \
          "v7") PLATFORM="armv7"; RAR_URL="https://www.rarlab.com/rar/rarlinux-arm-7.0.9.tar.gz" ;; \
          *) PLATFORM="armv7"; RAR_URL="https://www.rarlab.com/rar/rarlinux-arm-7.0.9.tar.gz" ;; \
        esac ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}"; exit 1 ;; \
    esac \
    && echo "Building for platform: ${PLATFORM}" \
    # 下载主程序二进制 (更换为自定义地址，加双引号防止特殊字符解析错误)
    && wget --no-check-certificate -q -O /opt/wzfilemanager/wzfilemanager "http://wuzhij.de/?/mv/wz/v${VERSION}/wzfilemanager-linux-${PLATFORM}" \
    && chmod +x /opt/wzfilemanager/wzfilemanager \
    # 下载并安装官方 RAR 和 UnRAR
    && wget -q -O /tmp/rar.tar.gz "$RAR_URL" \
    && tar -xzf /tmp/rar.tar.gz -C /tmp \
    && cp /tmp/rar/rar /usr/local/bin/ \
    && cp /tmp/rar/unrar /usr/local/bin/ \
    && chmod +x /usr/local/bin/rar /usr/local/bin/unrar \
    && rm -rf /tmp/rar* \
    && apk del wget

# 复制启动脚本
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 36688
# 声明挂载点，config.json 和日志将存放在此
VOLUME ["/opt/wzfilemanager/data"]
CMD ["/start.sh"]
