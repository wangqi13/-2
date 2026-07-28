# 物料组合方案求解工具

基于 OR-Tools CP-SAT 求解器的原料组合配比工具。上传 Excel → 设置约束 → 一键生成多个满足所有指标加权要求的物料组合方案。

## 快速上手

1. 下载 Release 中的 `物料组合方案.exe`，双击运行
2. 浏览器自动打开，上传 Excel（或用仓库里的 `sample_data.xlsx` 测试）
3. 设置总重范围、目标冻力上下限、单行冻力筛选范围
4. 点"开始求解"，结果页展示方案详情，可下载 Excel

> 源码运行：`pip install -r requirements.txt && python app.py`，浏览器访问 `http://127.0.0.1:5000`

## 功能特性

- **多维约束求解**：冻力、水分、灰分、勃氏粘度、透光率（450/620）加权平均约束
- **冻力自定义范围**：自由设定加权冻力上下限，不再固定 ±5%
- **多段范围筛选**：支持单行冻力多段区间筛选（如 `100-200, 300-400`）
- **多方案生成**：支持 1~10 个不重复方案，可选批道号完全不重叠
- **重量范围控制**：设上下限则随机分布，不设上限则贴近下限
- **整行取用**：每个原料行全取或全不取（0-1 布尔变量）
- **结果导出**：网页展示 + 下载 Excel 结果文件
- **局域网共享**：同一 WiFi 下多设备通过浏览器访问
- **便携 exe**：PyInstaller 打包为单文件，无需安装 Python

## 技术栈

- **后端**：Flask + Python 3.7+
- **求解器**：Google OR-Tools CP-SAT（0-1 整数规划）
- **数据处理**：pandas, openpyxl
- **前端**：原生 HTML/CSS/JavaScript（单页应用）
- **打包**：PyInstaller（生成单文件 exe）

## 安装与运行

### 方式一：直接运行 exe

从 Release 页面下载 `物料组合方案.exe`，双击即用。发给别人也只需要这一个文件。

### 方式二：源码运行

```bash
git clone https://github.com/wangqi13/-2.git
cd -2
pip install -r requirements.txt
python app.py
```

浏览器访问 `http://127.0.0.1:5000`

### 方式三：打包为 exe

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --add-data "templates;templates" --name "物料组合方案" app_desktop.py
```

exe 生成在 `dist/` 目录下。

## Excel 数据格式

上传的 Excel 必须包含以下列（支持空格和换行符清洗）：

| 列名 | 说明 |
|------|------|
| 批道 | 批次编号（仅标识，不参与计算） |
| 总数kg | 该行原料总重量 |
| 冻力 | 冻力指标值 |
| 水分 | 水分指标值 |
| 灰分 | 灰分指标值 |
| 勃氏粘度 | 勃氏粘度指标值 |
| 透光率450 | 透光率 450nm 指标值 |
| 透光率620 | 透光率 620nm 指标值 |

仓库中有 `sample_data.xlsx` 可直接用于测试。

## 求解算法

加权平均公式：

```
加权指标 = Σ(指标值 × 该行重量) / 总重
```

约束条件：

- 总重 ∈ [下限, 上限（可选）]
- 加权冻力 ∈ [下限, 上限]（用户自定义）
- 其他指标上下限（可选，加权计算）
- 整行取用（0-1 布尔变量）
- 方案间批道不重复（可选开关）

求解器：Google OR-Tools CP-SAT，每次迭代 10 秒超时，支持 4000+ 行数据规模。

## 项目结构

```
├── app.py                 # Flask 网页版主程序
├── app_desktop.py         # 桌面版主程序（打包用）
├── templates/
│   └── index.html         # 前端页面
├── sample_data.xlsx       # 示例数据
├── requirements.txt       # Python 依赖
├── LICENSE
├── .gitignore
└── README.md
```

## 部署

局域网内其他设备访问：`http://<本机IP>:5000`

首次使用需开放 Windows 防火墙 5000 端口（管理员权限）：

```bash
netsh advfirewall firewall add rule name="Flask 5000" dir=in action=allow protocol=TCP localport=5000
```

## License

MIT
