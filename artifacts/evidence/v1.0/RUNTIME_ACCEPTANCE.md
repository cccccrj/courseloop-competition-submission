# v1.0运行包验收口径

2026-10-09从427044e36cad9ddd96880b727bce1b80209d5c3c源码及对应干净前端构建生成候选运行包，解压至全新临时目录，使用全新Python3.10虚拟环境、清华源和requirements.txt安装，pip check通过。环境版本见runtime-environment.lock.txt。

源码回归604/604通过，0失败、0跳过；功能、权限及教学协同86/86通过；浏览器23/23通过，JS错误0；导学模型桩闭环与边界14/14通过，原创算法与检索检查见runtime-algorithms.json。原始证据分别为runtime-backend-junit.xml、runtime-backend-tests.log、functional-runtime.json、ui-final-runtime/、runtime-smoke.json。

该验收之后仅更新打包工具、报告生成工具、文档和证据，backend、frontend/src及tests保持一致。正式包重新构建前端，并以SUBMISSION_MANIFEST.json标识冻结源码，SHA256SUMS校验资产。发布时另外提供最终归档检查及下载后复核凭据，确认最终包的源代码、前端版本、原创数据和报告完整性。

正式提交使用v1.0，历史v1.0不作为当前提交包。当前正式报告仅汇总更新代码后的本轮完整验收结果，GPU和测试环境业务容量的源码与实验环境分别记录。
