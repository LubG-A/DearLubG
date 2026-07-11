# DearLubG
基于 NapCat 框架的 QQ 群聊机器人。

## 部署
需要安装 python， 以及 requirements.txt 中的依赖。

首先部署 NapCat ，请参考 https://github.com/NapNeko/NapCatQQ/ 中的教程。

部署完成后，按照如下步骤启动 NapCat 的 HTTP 服务器和客户端：

1.进入 “网络配置” 栏；
2.点击 “新建”，选择 “HTTP 服务器”，设定其端口为 8080；
3.点击 “新建”，选择 “HTTP 客户端”，设定其端口为 8081；
4.记得启动两个服务。

完成 NapCat 客户端和服务端的搭建后，在 config.yaml 中完成基础配置，至少需要配置 `token` 和 `group_ids` 两个参数。

最后，进入当前目录，运行 `python main.py` 即可启动机器人。