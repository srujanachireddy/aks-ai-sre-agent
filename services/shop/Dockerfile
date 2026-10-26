# One image for all three services. SERVICE_NAME picks the role at runtime.
# The "bad release" is the same code built with BUG_CURRENCY_KEYERROR=true:
#   docker build -t shop:v1.3.2 --build-arg APP_VERSION=v1.3.2 .
#   docker build -t shop:v1.4.0 --build-arg APP_VERSION=v1.4.0 --build-arg BUG_CURRENCY_KEYERROR=true .
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ARG APP_VERSION=dev
ARG BUG_CURRENCY_KEYERROR=false
ENV APP_VERSION=${APP_VERSION} \
    BUG_CURRENCY_KEYERROR=${BUG_CURRENCY_KEYERROR} \
    PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home shop
COPY --from=build /install /usr/local
USER 10001
EXPOSE 8080
ENTRYPOINT ["shop"]
