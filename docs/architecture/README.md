# 交互式架构导航

本目录随项目仓库分发。直接用浏览器打开 index.html；或从项目根目录运行 `python -m http.server 8765`，访问 `http://127.0.0.1:8765/docs/architecture/`。无需模型密钥或后端服务，页面不会调用模型或数据库。

包括三条业务流程、16 个节点、40 个中英术语、可搜索函数索引和源码快照。根目录 README / docs/design.md 描述当前项目，docs/verification.md 解释历史成绩与能力边界。

维护：先按代码变更核对 content.py，再从项目根目录执行：

```sh
python docs/architecture/build.py
python docs/architecture/validate.py
```

构建脚本用 Python AST 自动定位类与函数行号；source-manifest.json 记录源码哈希。validate.py 检查页面、链接、锚点和快照一致性。节点说明是人工维护，构建时间不代表自动更新语义说明。源码快照只包含引用的项目源文件，不包含密钥、原始模型运行消息或数据库。
