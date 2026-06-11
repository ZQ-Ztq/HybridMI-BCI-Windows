# BCI 脑机接口鼠标控制系统  v2.0

基于 OpenBCI **Cyton+Daisy** 16通道板的脑机接口鼠标控制软件，支持 **macOS (Apple M芯片)** 和 **Windows** 双平台。

---

## v2.0 更新内容

- **8点位脚部运动想象检测**：FC3/FC4/C3/C4（主检测 70%）+ F3/F4/CP3/CP4（备选增强 30%），加权投票
- **蓝牙连接支持**：软件自动搜索蓝牙接收器（BLED112），识别后手动连接
- **系统自检功能**：点击测试后鼠标画出一个小正方形并回到原点，验证控制正常，不依赖真实脑电波
- **CP3/CP4 通道启用**：此前禁用的第 7/14 通道（1-indexed）现已启用
- 版本号更新至 v2.0.0

---

## 通道分配表  v2.0

| 通道（1-indexed） | 内部索引 | 位置 | 用途 |
|:-:|:-:|:-:|:-:|
| CH1 | 0 | 眼轮匝肌外侧（左） | 眨眼 EMG |
| CH2 | 1 | 眼眶上方（左） | 眨眼 EMG |
| CH3 | 2 | Fp1 | 专注度 |
| CH4 | 3 | F3 | **备选** 脚部 MI |
| CH5 | 4 | FC3 | **主检测** 脚部 MI |
| CH6 | 5 | C3 | **主检测** 脚部 MI |
| CH7 | 6 | CP3 | **备选** 脚部 MI |
| CH8 | 7 | 眼轮匝肌外侧（右） | 眨眼 EMG |
| CH9 | 8 | 眼眶上方（右） | 眨眼 EMG |
| CH10 | 9 | Fp2 | 专注度 |
| CH11 | 10 | F4 | **备选** 脚部 MI |
| CH12 | 11 | FC4 | **主检测** 脚部 MI |
| CH13 | 12 | C4 | **主检测** 脚部 MI |
| CH14 | 13 | CP4 | **备选** 脚部 MI |
| CH15 | 14 | — | ⛔ 禁用 |
| CH16 | 15 | — | ⛔ 禁用 |

> **v2.0 8点位加权投票原理：**  
> 主检测点（FC3/FC4/C3/C4）权重 70%，备选点（F3/F4/CP3/CP4）权重 30%。  
> 分数 = 0.7×(主L−主R)/(主L+主R) + 0.3×(备L−备R)/(备L+备R)

---

## 控制逻辑  v2.0

### 鼠标移动（运动想象 ERD/ERS）

基于大脑**交叉控制**原理：

| 运动想象 | ERD 侧 | 检测依据 | 鼠标动作 |
|:-:|:-:|:-:|:-:|
| 想象**左脚** | 右侧运动皮层功率↓ | 总分 > +0.12 | ↓ 下移 |
| 想象**右脚** | 左侧运动皮层功率↓ | 总分 < −0.12 | ↑ 上移 |

ERD = Event-Related Desynchronization（事件相关去同步化）  
检测频段：**μ波（8-12 Hz）** + **Beta波（13-25 Hz）** 加权平均

### 鼠标点击（眨眼 EMG）

| 事件 | 触发条件 | 鼠标动作 |
|:-:|:-:|:-:|
| 单次有意眨眼 | EMG RMS > 150 µV（可调）| 鼠标左键单击 |
| 两次有意眨眼 | 两次 >150 µV，间隔 < 800 ms | 鼠标左键双击 |
| 普通眨眼 | EMG RMS 在 80-150 µV 之间 | 忽略 |

### 鼠标速度（专注度）

- 通过 Fp1/Fp2 计算 **β/(α+θ)** 注意力指数
- 专注度 0% → 速度 2 px/tick
- 专注度 100% → 速度 25 px/tick
- 使用 EMA 平滑避免抖动

---

## 安装与运行

### 方式一：直接从源码运行

```bash
# 1. 创建虚拟环境
python -m venv venv
source venv/bin/activate       # macOS/Linux
# 或 venv\Scripts\activate.bat  # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
python src/main.py
```

### 方式二：macOS .app 打包

```bash
chmod +x build_scripts/build_mac.sh
./build_scripts/build_mac.sh
# 输出: dist/mac/BCI_MouseControl.app
```

**macOS 权限配置（必须）：**
1. 系统设置 → 隐私与安全性 → **辅助功能** → 添加 BCI_MouseControl
2. 系统设置 → 隐私与安全性 → **输入监控** → 添加 BCI_MouseControl
3. 串口访问：`ls /dev/cu.*` 查看 OpenBCI 串口号

### 方式三：Windows .exe 打包

```cmd
build_scripts\build_windows.bat
:: 输出: dist\windows\BCI_MouseControl.exe
```

**Windows 权限配置（必须）：**
- 以**管理员身份**运行 .exe
- 安装 CH340/FTDI USB转串口驱动
- 在设备管理器中确认 COM 口号

---

## 运行测试

```bash
cd BCI_MouseControl
python tests/test_signal.py
```

---

## v2.0 新功能使用

### 蓝牙连接

1. 点击 **"🔍 搜索蓝牙设备"**（蓝牙标签页）
2. 软件自动扫描 BLED112 等蓝牙串行设备
3. 在设备列表中选择目标设备
4. 点击 **"📡 连接蓝牙设备"** 完成连接

### 系统自检

1. 点击右侧面板的 **"▶ 开始自检测试"**
2. 鼠标将自动画出一个 100×100 px 的正方形
3. 绘制完成后鼠标**回到原点**
4. 触控板控制自动恢复
5. 查看测试结果（漂移量）

> 💡 自检不依赖真实脑电波，是纯软件验证，可用于安装后快速确认鼠标控制功能正常。

---

## 硬件连接

1. OpenBCI Cyton + Daisy 扩展板组合
2. 按通道表贴放电极（导电膏或干电极）
3. USB 转串口 连接到电脑（或 BLED112 蓝牙 dongle）
4. 参考电极贴耳垂或乳突
5. 在软件串口下拉中选择对应 COM/cu.* 端口（或蓝牙标签页连接）

---

## 取消控制流程

1. 打开软件 → 点击 **"取消控制"** 按钮
2. 弹出确认对话框
   - 点击 **"确认取消控制"** → 鼠标恢复手动控制
   - 点击 **"继续脑机控制"** → 保持 BCI 控制

> ⚠️ 此流程专为患者场景设计：首次点击不立即生效，必须二次确认，防止误触。

---

## 技术栈

| 组件 | 技术 |
|:-:|:-:|
| 信号采集 | BrainFlow 5.x SDK |
| 信号处理 | NumPy + SciPy (Welch PSD, Butterworth) |
| GUI | PyQt6 |
| 鼠标控制 | pynput (跨平台) |
| 打包 | PyInstaller |
| 蓝牙 | 系统级扫描 + BLED112 支持 |

---

## 参数调节建议

在 `src/signal_processor.py` 中可调整：

```python
BLINK_THRESHOLD_LOW   = 80.0    # µV - 普通眨眼下限
BLINK_THRESHOLD_HIGH  = 150.0   # µV - 有意眨眼阈值
ERD_RATIO_THRESH      = 0.12    # ERD 不对称比阈值
ERD_CONFIRM_COUNT     = 3       # 需要连续确认次数
FOCUS_MIN_SPEED       = 2.0     # 最小鼠标速度 px/tick
FOCUS_MAX_SPEED       = 25.0    # 最大鼠标速度 px/tick
FOOT_MI_PRIMARY_WEIGHT = 0.70   # 主检测点权重
FOOT_MI_BACKUP_WEIGHT  = 0.30   # 备选点权重
```

也可在软件界面的滑块中实时调节眨眼阈值。
