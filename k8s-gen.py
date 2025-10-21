#!/usr/bin/env python3
import os
import sys
from textwrap import dedent


def main():
    image = os.getenv("IMAGE", "file-manager:latest")
    namespace = os.getenv("NAMESPACE", "default")
    name = os.getenv("APP_NAME", "file-manager")
    storage = os.getenv("STORAGE", "1Gi")
    storage_class = os.getenv("STORAGE_CLASS", "")
    port = int(os.getenv("PORT", "8000"))

    sc_line = f"  storageClassName: {storage_class}\n" if storage_class else ""

    pvc = f"""
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {name}-pvc
  namespace: {namespace}
spec:
  accessModes:
    - ReadWriteOnce
{sc_line}  resources:
    requests:
      storage: {storage}
"""

    deploy = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
        - name: {name}
          image: {image}
          imagePullPolicy: IfNotPresent
          env:
            - name: FILE_MANAGER_DEFAULT_ROOT
              value: /data
          ports:
            - containerPort: {port}
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: {name}-pvc
"""

    svc = f"""
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {namespace}
spec:
  type: ClusterIP
  selector:
    app: {name}
  ports:
    - name: http
      port: 80
      targetPort: {port}
"""

    sys.stdout.write(pvc.lstrip())
    sys.stdout.write("---\n")
    sys.stdout.write(deploy.lstrip())
    sys.stdout.write("---\n")
    sys.stdout.write(svc.lstrip())


if __name__ == "__main__":
    main()


