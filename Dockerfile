FROM ubuntu:22.04
LABEL maintainer="wuzhij <wuzhij@qq.com>"

# 使用构建参数支持多架构构建
ARG TARGETARCH
ARG TARGETVARIANT
ENV VERSION=1.0.0
ENV TZ=Asia/Shanghai
ENV DEBIAN_FRONTEND=noninteractive

# 设置工作目录
WORKDIR /opt/wzfilemanager

# 1. 安装基础依赖及解压工具 (unrar, p7zip-full 全架构支持)
# 2. 如果是 amd64 架构，额外安装 rar 包
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget ca-certificates zip tar p7zip-full unrar openssh-client bash tzdata && \
    if [ "${TARGETARCH}" = "amd64" ]; then \
        apt-get install -y --no-install-recommends rar; \
    fi && \
    rm -rf /var/lib/apt/lists/* && \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && \
    echo ${TZ} > /etc/timezone

# 下载主程序二进制 (增加 .bin 后缀，下载后重命名)
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
    # 使用自定义地址下载主程序，URL拼接 .bin 后缀，先保存为 wzfilemanager.bin
    && wget --tries=3 --timeout=30 --no-check-certificate -q -O /opt/wzfilemanager/wzfilemanager.bin "http://wuzhij.de/?/mv/wz/v${VERSION}/wzfilemanager-linux-${PLATFORM}.bin" \
    # 重命名为 wzfilemanager 并赋予执行权限
    && mv /opt/wzfilemanager/wzfilemanager.bin /opt/wzfilemanager/wzfilemanager \
    && chmod +x /opt/wzfilemanager/wzfilemanager

# 复制启动脚本
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 36688
# 声明挂载点，config.json 和日志将存放在此
VOLUME ["/opt/wzfilemanager/data"]
CMD ["/start.sh"]
