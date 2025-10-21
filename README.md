# 文件管理器

一个轻量的跨平台 Web 文件管理器，支持上传/下载/删除/重命名文件，创建/删除/重命名目录。基于 FastAPI，HTTP 通信，提供 Docker 构建，并可自动生成 Kubernetes 清单。

## 本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 可选：设置根目录（默认 ./data）
export FILE_MANAGER_ROOT=$(pwd)/data
python -m app.main
# 打开浏览器访问 http://localhost:8000
```

## Docker 构建与运行

```bash
docker build -t virtisos:latest .
# 挂载宿主目录到容器 /data
mkdir -p ./data
docker run --rm -it -p 8000:8000 -v $(pwd)/data:/data virtisos:latest
```

## 生成 Kubernetes YAML

```bash
# 环境变量可覆盖：IMAGE、NAMESPACE、APP_NAME、STORAGE、STORAGE_CLASS、PORT
python k8s-gen.py > k8s.yaml
# 应用到集群
kubectl apply -f k8s.yaml
```

## API 简述
- GET `/api/list?path=`：列目录
- GET `/api/download?path=`：下载文件
- POST `/api/upload?path=`：上传文件（FormData: files[]）
- POST `/api/mkdir`：{ path, name }
- POST `/api/rename`：{ path, oldName, newName }
- POST `/api/delete`：{ path, name }（目录需为空）

## 界面
- 简洁表格样式，支持进入目录、返回上级、上传、下载、删除、重命名、新建目录。

## 注意
- 所有路径均在根目录沙箱内，禁止越权访问。
- 删除目录仅允许删除空目录，避免误删。


