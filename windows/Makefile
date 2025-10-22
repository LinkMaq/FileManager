# Makefile for FileManager project
# Usage examples:
#  make build            # build docker image
#  make run              # run container (default port 8000)
#  make stop             # stop and remove container
#  make logs             # tail container logs
#  make push             # push image to registry (set IMAGE)
#  make clean            # remove local image

IMAGE ?= registry.cn-hangzhou.aliyuncs.com/xiaoshiai/windows
TAG ?= 5.11
PORT ?= 8000
CONTAINER_NAME ?= windows
VOLUME ?= $(shell pwd)/data:/data

.PHONY: help build run stop logs push clean

help:
	@echo "Available targets: build run stop logs push clean"
	@echo "Variables: IMAGE=$(IMAGE) TAG=$(TAG) PORT=$(PORT) CONTAINER_NAME=$(CONTAINER_NAME)"

build:
	docker build -t $(IMAGE):$(TAG) .

push:
	docker push $(IMAGE):$(TAG)

clean:
	docker image rm -f $(IMAGE):$(TAG) || true

# convenience target for building and running
rebuild: clean build run
	@echo "Rebuilt and started container '$(CONTAINER_NAME)'"
