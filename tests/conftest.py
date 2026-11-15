import os
import tempfile

# 在导入 app.main 之前指定数据库位置，避免模块级 app 在仓库目录生成 app.db
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="wm-global-"), "app.db")
