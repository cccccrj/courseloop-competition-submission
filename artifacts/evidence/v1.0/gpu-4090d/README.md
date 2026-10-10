# 4090D正式实测证据

本目录对应2026-10-09独立云GPU部署，不改变在线演示环境服务。

- benchmark.json：4组性能负载、70个计时请求、逐次输入／输出、token用量、GPU采样，以及4门课程真实客户端自然结束的回答。
- inventory.json：硬件、驱动、容器配额、依赖版本及逐文件模型SHA256；权重不随Git分发。
- service-command.json：实际模型服务启动参数。
- gpu-environment.lock.txt、app-environment.lock.txt：两套独立虚拟环境的完整依赖。
- functional.json：86项功能、权限和跨角色协同验收。
- ui/：23项浏览器验收及截图，JS错误0。
- application-sse.json：2个真实应用请求的材料检索、流式回答和结束事件。
- *.log：本次正式完成的测试日志。

应用源码快照为3a265650eb7cdd715adcb3a9c78855bffccdd8be；实际GPU测试脚本SHA256为54d1570fb3e1b95a03143b394b391d9d6bd5ede7c7d663c76a65a5a11956336c，与benchmark.json及仓库脚本一致。该脚本在快照之后完善了全批预热和摘要记录，应用业务代码未改变。编译前端同样基于3a26565，构建时间2026-10-09T11:31:49Z。

源码回归604项通过的JUnit和日志在上级backend-junit.xml、backend-tests.log，执行环境为此云服务器的应用虚拟环境。测试环境10／25／50用户压测覆盖的事务、数据库与掌握度模块与本次部署一致；原正式业务容量证据继续保留。

固定128／256输出token用于性能可比性（ignore_eos=true），自然结束的问答用于应用链路验收；两类指标分别统计。GPU HTTP计时包含本地排队与服务开销，并非纯CUDA内核计时。4门课程调用为小样例接入检查，统计准确率使用独立标注评价。
