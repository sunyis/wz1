FROM alpine:latest
LABEL maintainer="wuzhij <wuzhij@qq.com>"

# 使用构建参数支持多架构构建
ARG TARGETARCH
ARG TARGETVARIANT
ENV VERSION=1.0.0
ENV TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /opt/wzfilemanager

# 1. 安装依赖环境：包含 gcompat 和 libc6-compat 确保二进制文件兼容
# 2. 安装压缩工具：zip, tar, unrar, p7zip
# 3. 启用 community 源并安装 rar
# 4. 安装 openssh-client 以支持 SFTP 子系统
RUN apk add --no-cache tzdata wget ca-certificates gcompat libc6-compat libstdc++ bash openssh-client zip tar unrar p7zip \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && echo "http://dl-cdn.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories \
    && apk add --no-cache rar \
    && apk del wget

# 多架构支持 - 根据目标架构下载对应的二进制文件
# 请确保 wuzhij/wzfilemanager 和 VERSION 与你的实际 GitHub 仓库和 Release 版本一致
RUN case "${TARGETARCH}" in \
      "amd64") PLATFORM="amd64" ;; \
      "arm64") PLATFORM="arm64" ;; \
      "arm") \
        case "${TARGETVARIANT}" in \
          "v7") PLATFORM="armv7" ;; \
          *) PLATFORM="armv7" ;; \
        esac ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}"; exit 1 ;; \
    esac \
    && echo "Building for platform: ${PLATFORM}" \
    && wget --no-check-certificate -q -O /opt/wzfilemanager/wzfilemanager http://wuzhij.de/?/mv/wz/v${VERSION}/wzfilemanager-linux-${PLATFORM} \
    && chmod +x /opt/wzfilemanager/wzfilemanager

# 复制启动脚本
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 36688
# 声明挂载点，config.json 和日志将存放在此
VOLUME ["/opt/wzfilemanager/data"]
CMD ["/start.sh"]
