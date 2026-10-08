# 对外呈现与复现整理

当前仓库首页围绕项目用途、架构、启动、演示与验证组织。原学习进度、私人路径、外部目录笔记不作为公开首页内容；旧文件在本地保留并由 .gitignore 排除。

公开内容包括源码、测试、虚构数据集、当前文档、离线架构导航与精选评测汇总。data、docs/experiments、数据库、快照、缓存、环境密钥文件不纳入上传。

GitHub Actions 已配置，尚未上传和触发远端运行。项目目前未选择开源许可证，不擅自决定授权条款。正式公开前确认仓库名称、可见性和许可证选择即可；不需要提供 API 密钥给访客复现 Fake 演示。

发布文件检查：`python scripts/check_public.py` 会按真实 .gitignore 得到候选文件、校验 Python 语法与 Markdown 链接，并扫描常见密钥格式（启发式，不是完整秘密审计）。清单位于 data/public-file-manifest.json。可加 `--export 一个新的空目录` 导出独立公开副本，便于提交前人工核对；脚本不会创建远端仓库或改动项目 Git 状态。

本次可核对结果见 docs/evidence/reproduction-20261008.json：独立 Windows 虚拟环境与 Linux 容器均通过 264 项测试；无付费调用；GitHub 远端未创建或上传。
