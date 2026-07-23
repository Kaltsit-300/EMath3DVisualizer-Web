# 方程可视化 · 3D 计算器

一个基于Web的3D数学方程可视化工具，支持实时渲染复杂的数学曲面和曲线。

## ✨ 功能特性

### 🎯 核心功能
- **3D方程可视化** - 支持隐式和显式方程的3D渲染
- **多方程叠加** - 同时显示多个方程，支持曲面交线计算
- **交互式控制** - 鼠标拖拽旋转、缩放、平移视角
- **实时参数调整** - 动态修改方程参数并实时更新渲染
- **智能坐标轴** - 自适应刻度显示，支持动态精度调整

### 🔧 技术栈
- **前端**：Three.js + 原生JavaScript + CSS Grid
- **后端**：FastAPI + SymPy + NumPy + Scikit-image
- **算法**：Marching Cubes等值面提取

## 🚀 快速开始

### 环境要求
- Python 3.8+
- 现代浏览器（支持WebGL）

### 安装步骤

1. **克隆项目**
```bash
git clone https://github.com/your-username/EMath3DVisualizer-Web.git
cd EMath3DVisualizer-Web
```

2. **安装Python依赖**
```bash
pip install -r requirements_api.txt
```

3. **启动服务器**
```bash
python api_server.py
```

4. **打开浏览器**
访问 `http://127.0.0.1:8006`

## 📖 使用指南

1. **输入方程** - 在输入框中输入数学方程，如 `z = x^2 + y^2`
2. **添加方程** - 点击"添加"按钮将方程添加到列表
3. **绘制图形** - 点击"绘制"按钮开始渲染
4. **交互控制** - 鼠标拖拽旋转，滚轮缩放，右键平移
5. **调整参数** - 动态修改方程参数查看效果

## 📁 项目结构

```
EMath3DVisualizer-Web/
├── webapp_index.html          # 主页面HTML结构
├── webapp_app.js              # 前端核心逻辑
├── webapp_styles.css          # 样式文件
├── api_server.py              # FastAPI服务器
├── mesh_service.py            # 网格生成服务
├── requirements_api.txt       # Python依赖列表
└── README.md                  # 项目说明文档
```

## 🤝 贡献

欢迎提交Issue和Pull Request！

## 📄 许可证

本项目采用MIT许可证。