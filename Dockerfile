FROM alpine:latest
LABEL maintainer="wuzhij <wuzhij@qq.com>"

# 使用构建参数支持多架构构建
ARG TARGETARCH
ARG TARGETVARIANT
ENV VERSION=1.0.0
ENV TZ=Asia/Shanghai

WORKDIR /opt

# 安装依赖环境，包含 gcompat 和 libc6-compat 以确保 PyInstaller 二进制文件兼容
RUN apk add --no-cache tzdata wget ca-certificates gcompat libc6-compat libstdc++ bash \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone

# 多架构支持 - 根据目标架构下载对应的二进制文件
# 注意：请将 wuzhij/wzfilemanager 替换为你的实际 GitHub 仓库地址
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
    && wget --no-check-certificate -q -O /opt/wzfilemanager https://github.com/wuzhij/wzfilemanager/releases/download/v${VERSION}/wzfilemanager-linux-${PLATFORM} \
    && chmod +x /opt/wzfilemanager \
    && apk del wget

# 创建数据目录
RUN mkdir -p /opt/data

# 复制启动脚本
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 36688
VOLUME ["/opt/data"]
CMD ["/start.sh"]
