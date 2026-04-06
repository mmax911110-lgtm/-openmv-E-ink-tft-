# OpenMV Cyber-Track HUD (Red Blob + E-Ink Status)
# ------------------------------------------------
# 红色目标追踪 + 墨水屏状态看板核心逻辑

import sensor, image, time, display
from pyb import SPI, Pin

# 1. 硬件引脚配置 (沿用无冲突方案)
RED_THRESHOLD = (30, 100, 15, 127, 15, 127) # LAB 红色阈值

def eink_init_bus():
    spi = SPI(2, SPI.MASTER, baudrate=2000000, polarity=0, phase=0)
    cs = Pin('P4', Pin.OUT_PP)
    dc = Pin('P5', Pin.OUT_PP)
    rst = Pin('P9', Pin.OUT_PP)
    
    # 物理复位
    rst.high(); time.sleep_ms(100); rst.low(); time.sleep_ms(20); rst.high(); time.sleep_ms(100)
    
    def send(cmd, data=None):
        dc.low(); cs.low(); spi.send(cmd); cs.high()
        if data is not None:
            dc.high(); cs.low()
            if isinstance(data, int): spi.send(data)
            else: spi.send(data)
            cs.high()

    # 初始化序列 (SSD1681 V2)
    send(0x12) # SWRESET
    time.sleep_ms(300)
    send(0x01, bytearray([0xC7, 0x00, 0x00])) # Driver output
    send(0x11, 0x03) # Data entry mode
    send(0x44, bytearray([0x00, 0x18])) # X RAM 0-24
    send(0x45, bytearray([0x00, 0x00, 0xC7, 0x00])) # Y RAM 0-199
    send(0x3C, 0x01) # Border
    send(0x22, 0xB1) # Load Waveform
    send(0x20)
    time.sleep_ms(200)
    
    return spi, cs, dc, send

def update_eink_text(spi, cs, dc, sender, text, symbol="", is_full=False):
    """
    在墨水屏上渲染实时 HUD 数据
    is_full: True 为全刷 (清屏), False 为局刷 (快速无闪烁)
    """
    canvas = image.Image(180, 100, sensor.GRAYSCALE, copy_to_fb=True)
    canvas.clear()
    
    # 渲染居中的数据信息
    lines = text.split("\n")
    char_w = 14 # scale=2 时每个字符的大致像素宽度
    for i, line in enumerate(lines):
        text_w = len(line) * char_w
        x_pos = (180 - text_w) // 2
        y_pos = 30 + (i * 28) # 每行间隔 28 像素
        canvas.draw_string(max(0, x_pos), y_pos, line, color=255, scale=2)

    if symbol:
        # 符号也居中显示 (180 - 16) // 2 = 82
        canvas.draw_string(82, 5, symbol, color=255, scale=2)
    
    # 构建 200x200 全屏 Buffer (5000 字节)
    buf = bytearray([0xFF] * 5000)
    
    # 搬运位图到 Buffer —— 稍微下移 (Offset 从 5 改为 12), 避开顶部缝隙遮挡
    for y in range(100):
        row_offset = (y + 12) * 25 
        for x in range(180):
            p = canvas.get_pixel(x, y)
            if p > 128:
                buf[row_offset + 1 + (x // 8)] &= ~(0x80 >> (x % 8))

    # SPI 发送数据到墨水屏
    spi.init(SPI.MASTER, baudrate=2000000)
    sender(0x4E, 0x00)
    sender(0x4F, bytearray([0x00, 0x00]))
    sender(0x24, buf)
    
    # 【核心修复】全屏刷新时必须同步写入寄存器 0x26 (Old Data RAM)
    # 否则在多次局部刷新后，全刷会因为差分检测导致画面重合或残影
    if is_full:
        sender(0x4E, 0x00)
        sender(0x4F, bytearray([0x00, 0x00]))
        sender(0x26, buf)
    
    # 执行物理刷新: 0xF7 全刷模式, 0xFF 局刷模式 (SSD1681 极速刷新)
    sender(0x22, 0xF7 if is_full else 0xFF)
    sender(0x20) 
    
    # 【修复卡顿核心】彻底去掉阻塞延时，只给 10ms 确保指令发出
    # 屏幕翻转由硬件后台完成，不再占用 OpenMV 处理时间
    time.sleep_ms(10)
    # 恢复 SPI 给 TFT 使用（如果共用总线）
    spi.init(SPI.MASTER, baudrate=54000000)

def main():
    # 1. 摄像头与彩屏初始化
    sensor.reset()
    sensor.set_pixformat(sensor.RGB565)
    sensor.set_framesize(sensor.QQVGA2) # 128x160
    sensor.skip_frames(time=2000)
    
    lcd = display.SPIDisplay() # 默认 CS=P3
    
    # 2. 墨水屏总线初始化
    spi, cs, dc, sender = eink_init_bus()
    
    # 动画与状态参数
    symbols = ["|", "/", "-", "\\"]
    sym_idx = 0
    current_state = 0   # 0: Searching, 1: Locked
    last_state = 0      # 追踪状态同步位
    last_refresh_time = time.ticks_ms() - 5000
    last_refresh_full = True 
    lock_timer = 0
    lost_timer = 0
    partial_count = 0 
    lock_confirmed = False # 锁定确认标记
    force_full_refresh = False # 强制全刷新标记 (用于切色)
    
    # === 多色追踪配置项 ===
    # 颜色库：[名称, LAB阈值, RGB显示颜色]
    COLORS = [
        ["RED",   (30, 100, 15, 127, 15, 127),    (255,   0,   0)],
        ["GREEN", (30, 100, -64, -8, -32, 32),    (  0, 255,   0)],
        ["BLUE",  (0, 30, 0, 64, -128, -20),      (  0,   0, 255)]
    ]
    target_idx = 0 # 当前索引 (0:红, 1:绿, 2:蓝)
    cover_start = 0 # 镜头遮挡计时
    
    print("DualView OpenMV System Started")
    
    # 开机首刷：确保屏幕背景纯净并显示初始状态
    update_eink_text(spi, cs, dc, sender, "SEARCHING\n" + COLORS[target_idx][0], is_full=True)
    last_refresh_time = time.ticks_ms()
    last_refresh_full = True # 标记为全刷，保护前 2 秒物理翻转期
    
    while True:
        img = sensor.snapshot()
        now = time.ticks_ms()
        
        # 0. 遮挡检测逻辑 (手势切换)
        stats = img.get_statistics()
        if stats.mean() < 8: # 如果画面极暗 (被手完全遮挡)
            if cover_start == 0: 
                cover_start = now
            elif time.ticks_diff(now, cover_start) > 2000: # 遮住持续满 2 秒
                target_idx = (target_idx + 1) % len(COLORS)
                last_state = 0
                current_state = 0
                partial_count = 0
                lock_confirmed = False
                force_full_refresh = True # 标记需要全屏刷新
                last_refresh_time = 0      # 强制刷新墨水屏
                
                cover_start = 0 
                lock_timer = 0
                print("[CMD] Switch Target: %s" % COLORS[target_idx][0])
                # 让画面黑屏一段时间示意切换成功
                time.sleep_ms(300) 
                continue 
            # 注意：在不满 2 秒且持续遮挡时，不要重置 cover_start
        else:
            cover_start = 0 # 只有光线恢复时才重置计时器

        # 1. 色块检测与状态切换
        blobs = img.find_blobs([COLORS[target_idx][1]], pixels_threshold=200, area_threshold=200, merge=True)
        
        if blobs:
            lost_timer = 0 
            if lock_timer == 0:
                lock_timer = now 
                sensor.set_auto_whitebal(True)
                sensor.set_auto_exposure(True)
                sensor.set_auto_gain(True)
            
            if time.ticks_diff(now, lock_timer) > 300:
                if current_state == 0:
                    sensor.set_auto_whitebal(False)
                    sensor.set_auto_exposure(False)
                    sensor.set_auto_gain(False)
                current_state = 1
            else:
                current_state = 0
        else:
            if last_state == 1:
                if lost_timer == 0: lost_timer = now
                if time.ticks_diff(now, lost_timer) < 100:
                    current_state = 1
                else:
                    current_state = 0
            else:
                current_state = 0
            
            if current_state == 0:
                sensor.set_auto_whitebal(True)
                sensor.set_auto_exposure(True)
                sensor.set_auto_gain(True)
                lock_timer = 0
                lock_confirmed = False
        
        # 2. 屏幕绘制逻辑
        display_color = COLORS[target_idx][2]
        target_name = COLORS[target_idx][0]
        if blobs:
            target = max(blobs, key=lambda b: b.pixels())
            img.draw_rectangle(target.rect(), color=display_color, thickness=1)
            img.draw_cross(target.cx(), target.cy(), color=display_color, size=5, thickness=1)
            if lock_confirmed:
                img.draw_string(2, 98, "LOCKED: %s" % target_name, color=display_color, scale=1)
                img.draw_string(2, 110, "X:%d Y:%d" % (target.cx(), target.cy()), color=display_color, scale=1)
            else:
                img.draw_string(2, 98, "SEARCHING\n%s" % target_name, color=display_color, scale=1)
        else:
            img.draw_string(2, 98, "SEARCHING\n%s" % target_name, color=display_color, scale=1)
            
        lcd.write(img)
        
        # 3. 墨水屏复合状态机
        busy_threshold = 2000 if last_refresh_full else 600
        
        # 增加锁定后与丢失后的稳定期 (稳定性优化)
        LOCK_STABLE_DELAY = 500
        LOST_STABLE_DELAY = 500

        # 情况 A: 强制全刷新 (用于切换目标)
        if force_full_refresh and time.ticks_diff(now, last_refresh_time) > busy_threshold:
            info = "SEARCHING\n%s" % COLORS[target_idx][0]
            update_eink_text(spi, cs, dc, sender, info, symbol="", is_full=True)
            last_refresh_full = True
            last_refresh_time = now
            force_full_refresh = False
            lock_confirmed = False

        # 情况 B: 从锁定变为丢失 (返回搜索，增加稳定延迟)
        elif current_state == 0 and last_state == 1:
            # 丢失目标后先稳定感光，此期间保持锁定画面，等待 500ms 后全刷新
            if time.ticks_diff(now, lost_timer) > (100 + LOST_STABLE_DELAY):
                # 状态转换必须等待前序操作彻底完成 (2s 为全屏刷新的安全物理周期)
                if time.ticks_diff(now, last_refresh_time) > busy_threshold:
                    info = "SEARCHING\n%s" % COLORS[target_idx][0]
                    update_eink_text(spi, cs, dc, sender, info, symbol=symbols[sym_idx], is_full=True)
                    last_refresh_full = True
                    last_refresh_time = now
                    last_state = 0
                    lock_confirmed = False

        # 情况 C: 锁定状态首次刷新 (增加 500ms 延迟)
        elif current_state == 1 and not lock_confirmed:
            # 锁定 300ms 后锁定传感器，再等 500ms 刷新墨水屏
            if time.ticks_diff(now, lock_timer) > (300 + LOCK_STABLE_DELAY):
                if time.ticks_diff(now, last_refresh_time) > busy_threshold:
                    if blobs:
                        target = max(blobs, key=lambda b: b.pixels())
                        info = "LOCKED\n%s\nX:%d Y:%d" % (COLORS[target_idx][0], target.cx(), target.cy())
                        update_eink_text(spi, cs, dc, sender, info, symbol="", is_full=True)
                        last_refresh_full = True
                        last_refresh_time = now
                        lock_confirmed = True
                        last_state = 1

        # 情况 E: 锁定状态中的坐标动态更新 (局部刷新)
        elif current_state == 1 and lock_confirmed and time.ticks_diff(now, last_refresh_time) > max(600, busy_threshold):
            if blobs:
                target = max(blobs, key=lambda b: b.pixels())
                info = "LOCKED\n%s\nX:%d Y:%d" % (COLORS[target_idx][0], target.cx(), target.cy())
                update_eink_text(spi, cs, dc, sender, info, symbol="", is_full=False)
                last_refresh_full = False
                last_refresh_time = now

        # 情况 D: 等待搜索时的动画刷新
        elif current_state == 0 and time.ticks_diff(now, last_refresh_time) > max(800, busy_threshold):
            sym_idx = (sym_idx + 1) % len(symbols)
            force_full = (partial_count >= 20)
            info = "SEARCHING\n%s" % COLORS[target_idx][0]
            update_eink_text(spi, cs, dc, sender, info, symbol=symbols[sym_idx], is_full=force_full)
            last_refresh_full = force_full 
            last_refresh_time = now
            partial_count = 0 if force_full else (partial_count + 1)
            last_state = 0
            lock_confirmed = False

if __name__ == "__main__":
    main()
