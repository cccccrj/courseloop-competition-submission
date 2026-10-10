# 访问方式与本地演示账号

本地演示入口：http://127.0.0.1:8000/

线上体验地址与评审凭据通过比赛指定渠道单独提供，不随公开材料分发。

线上评审凭据通过比赛指定渠道单独提供，不包含在源码、报告或运行包中。以下账号仅适用于本地下载运行包及一次性测试副本，不能作为线上登录凭据。

| 账号 | 角色 | 密码 | 可用环境 |
|---|---|---|---|
| competition-student | student | CourseLocal-Student-2026! | 本地演示 |
| competition-student2 | student | CourseLocal-Student2-2026! | 本地演示 |
| competition-ta | ta | CourseLocal-Ta-2026! | 本地演示 |
| competition-teacher | teacher | CourseLocal-Teacher-2026! | 本地演示 |
| competition-admin | admin | CourseLocal-Admin-2026! | 本地演示 |

以上为公开的虚构演示身份。默认 demo_student／demo_ta 的密码为 123456，也仅供回环地址演示。部署到公网前，请修改或停用全部演示账号，生成独立签名密钥，配置 HTTPS 和访问控制。

运行包只包含处理后课程内容和虚构身份；初始个人作答、学习会话及教学协同记录为空。多人协同测试应使用不同本地身份，压力写入仅在独立测试副本执行。
