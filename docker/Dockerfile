# Runs the jobs in .github/workflows/ci.yml locally.
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*


# The Frappe Linter and Code Quality jobs.
FROM base AS quality

# Versions match the rev pins in .pre-commit-config.yaml.
RUN pip install --no-cache-dir \
        isort==8.0.1 \
        black==26.5.1 \
        flake8==7.3.0 \
        pylint==4.0.6 \
        mypy==2.3.0 \
        pre-commit

COPY docker/lib-checkout.sh docker/run-quality-job.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/run-quality-job.sh

CMD ["run-quality-job.sh"]


# The Semgrep Rules and Vulnerable Dependency Check jobs.
# Semgrep pins its own dependency tree, so it installs apart from the lint tools.
FROM base AS security

RUN pip install --no-cache-dir semgrep pip-audit

COPY docker/lib-checkout.sh docker/run-security-job.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/run-security-job.sh

CMD ["run-security-job.sh"]


# The Server job.
FROM base AS bench

ARG NODE_VERSION=24

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cron \
        libffi-dev \
        libjpeg-dev \
        libmariadb-dev \
        libssl-dev \
        mariadb-client \
        pkg-config \
        redis-tools \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g yarn \
    && rm -rf /var/lib/apt/lists/*

COPY docker/lib-checkout.sh docker/run-server-job.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/run-server-job.sh

# bench refuses to run as root. uid 1000 matches the mounted checkout.
RUN useradd --create-home --uid 1000 --shell /bin/bash runner
USER runner
WORKDIR /home/runner
ENV PATH="/home/runner/.local/bin:${PATH}"

RUN pip install --no-cache-dir --user frappe-bench

# Docker seeds a named volume from the image, so the cache mount keeps this owner.
RUN mkdir -p /home/runner/.cache

CMD ["run-server-job.sh"]
