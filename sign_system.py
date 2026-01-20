import json
import time
import calendar
import aiohttp
import asyncio
import os
from datetime import datetime, date
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

class SignSystem:
    def __init__(self, data_dir: Path):
        self.data_file = data_dir / "checkins.json"
        self.holiday_cache_file = data_dir / "holidays.json"
        self.data = {}
        self.holidays = {}
        self._load_data()
        self._load_holidays()
        
        # 字体配置 - 优先使用本地 fonts 目录
        current_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        self.font_path_text = current_dir / "fonts" / "text.ttf"
        
        # 如果本地没有，尝试系统字体
        if not self.font_path_text.exists():
             self.font_path_text = "C:/Windows/Fonts/msyh.ttc"
             if not os.path.exists(self.font_path_text):
                 self.font_path_text = "C:/Windows/Fonts/msyh.ttf"
        
    def _load_data(self):
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
            except:
                self.data = {}
        else:
            self.data = {}

    def _save_data(self):
        with open(self.data_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _load_holidays(self):
        if self.holiday_cache_file.exists():
            try:
                with open(self.holiday_cache_file, 'r', encoding='utf-8') as f:
                    self.holidays = json.load(f)
            except:
                self.holidays = {}
    
    def _save_holidays(self):
        with open(self.holiday_cache_file, 'w', encoding='utf-8') as f:
            json.dump(self.holidays, f, ensure_ascii=False, indent=2)

    async def get_holidays(self, year: int):
        str_year = str(year)
        # 如果缓存里有，且不是空字典，直接返回（可以按需增加过期机制）
        if str_year in self.holidays and self.holidays[str_year]:
             return self.holidays[str_year]
        
        url = f"https://api.jiejiariapi.com/v1/holidays/{year}"
        print(f"Fetching holidays from {url}")
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.holidays[str_year] = data
                        self._save_holidays()
                        return data
                    else:
                        print(f"API Error: {resp.status}")
            except Exception as e:
                print(f"Failed to fetch holidays: {e}")
        return {}

    def sign_in(self, user_id: str):
        today = date.today().isoformat() # YYYY-MM-DD
        user_id = str(user_id)
        
        if user_id not in self.data:
            self.data[user_id] = []
        
        if today in self.data[user_id]:
            return False, "今天已经打过卡了哦~"
            
        self.data[user_id].append(today)
        self._save_data()
        return True, "打卡成功！"

    async def draw_calendar_image(self, user_id: str):
        """生成日历图片"""
        now = datetime.now()
        year = now.year
        month = now.month
        today_date_str = now.strftime("%Y-%m-%d")
        
        # 获取节假日数据
        holidays_data = await self.get_holidays(year)
        
        # 基础配置
        cell_size = 100
        padding = 10
        header_height = 80
        days_header_height = 60
        cols = 7
        rows = 6 # 最多6行 (5行不够的情况: 1号是周六且是大月)
        
        width = (cell_size + padding) * cols + padding
        height = header_height + days_header_height + (cell_size + padding) * rows + padding
        
        # 颜色定义
        bg_color = "#FFCC66" # 橙黄色背景
        cell_bg_color = "#FFFFF0" # 象牙白
        cell_today_bg_color = "#FFFACD" # 柠檬绸色 (今天的背景稍微不同?)
        text_color = "#8B4513" # 马鞍棕色
        circle_color = "#2EB82E" # 绿色圆圈
        holidays_text_color = "#CD5C5C" # 印度红 (周末/节假日)

        image = Image.new('RGB', (width, height), bg_color)
        draw = ImageDraw.Draw(image)
        
        try:
            # 增大字体大小 40->60, 30->40
            font_path = str(self.font_path_text)
            font_large = ImageFont.truetype(font_path, 60)
            font_medium = ImageFont.truetype(font_path, 40)
            font_small = ImageFont.truetype(font_path, 30)
            font_holiday = ImageFont.truetype(font_path, 20)
        except:
             # Fallback if font load fails
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = ImageFont.load_default()
            font_holiday = ImageFont.load_default()

        # 1. 绘制顶部 202x年x月
        title_text = f"{year}年{month}月"
        # 居中
        # Pillow 9.2.0 textbbox, older textsize
        try:
            bbox = draw.textbbox((0, 0), title_text, font=font_large)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        except AttributeError:
            text_w, text_h = draw.textsize(title_text, font=font_large)
            
        draw.text(((width - text_w) / 2, (header_height - text_h) / 2), title_text, fill=text_color, font=font_large)

        # 2. 绘制星期头
        weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
        # Python calendar 0 is Monday, 6 is Sunday. 
        # But we want Sunday first (0). 
        # API/Calendar standard mapping:
        # calendar.monthcalendar returns lists of ints. 
        # By default calendar.firstweekday is 0 (Monday).
        # We need to set firstweekday to 6 (Sunday) to match the image.
        cal = calendar.Calendar(firstweekday=6)
        month_days = cal.monthdayscalendar(year, month)
        
        for i, w in enumerate(weekdays):
            x = padding + i * (cell_size + padding)
            y = header_height
            # Center text in cell width
            try:
                bbox = draw.textbbox((0, 0), w, font=font_medium)
                tw = bbox[2] - bbox[0]
            except:
                tw, th = draw.textsize(w, font=font_medium)
            
            draw.text((x + (cell_size - tw)/2, y + 15), w, fill=text_color, font=font_medium)

        # 3. 绘制日期格子
        start_y = header_height + days_header_height
        
        user_logs = self.data.get(str(user_id), [])
        
        row_idx = 0
        for week in month_days:
            col_idx = 0
            for day in week:
                x = padding + col_idx * (cell_size + padding)
                y = start_y + row_idx * (cell_size + padding)
                
                # 绘制格子背景 (圆角矩形)
                if day != 0:
                    shape_box = [x, y, x + cell_size, y + cell_size]
                    draw.rounded_rectangle(shape_box, radius=10, fill=cell_bg_color)
                    
                    # 日期字符串
                    current_date_str = f"{year}-{month:02d}-{day:02d}"
                    
                    # 检查是否节假日/周末
                    is_off_day = False
                    holiday_name = ""
                    # API check
                    if current_date_str in holidays_data:
                        h_info = holidays_data[current_date_str]
                        if h_info.get("isOffDay"):
                            is_off_day = True
                        holiday_name = h_info.get("name", "")
                    else:
                        # Fallback: simple weekend check if no data
                        # weekday(): Mon=0, Sun=6
                        dt = date(year, month, day)
                        if dt.weekday() >= 5: # Sat, Sun
                            is_off_day = True

                    # 颜色
                    num_color = text_color
                    if is_off_day:
                        num_color = holidays_text_color
                    
                    # 绘制数字
                    day_str = str(day)
                    try:
                        bbox = draw.textbbox((0, 0), day_str, font=font_large)
                        dw = bbox[2] - bbox[0]
                        dh = bbox[3] - bbox[1]
                    except:
                        dw, dh = draw.textsize(day_str, font=font_large)
                    
                    # 稍微偏上的居中
                    text_y_offset = (cell_size - dh)/2 - 5
                    if holiday_name:
                        text_y_offset -= 10 # 如果有节日名称，数字稍微往上挪一点
                    
                    draw.text((x + (cell_size - dw)/2, y + text_y_offset), day_str, fill=num_color, font=font_large)
                    
                    # 绘制节日名称 (如果有)
                    if holiday_name:
                         try:
                            bbox = draw.textbbox((0, 0), holiday_name, font=font_holiday)
                            hw = bbox[2] - bbox[0]
                            hh = bbox[3] - bbox[1]
                         except:
                            hw, hh = draw.textsize(holiday_name, font=font_holiday)
                         
                         draw.text((x + (cell_size - hw)/2, y + cell_size - hh - 5), holiday_name, fill=holidays_text_color, font=font_holiday)

                    # 如果打卡了，画个绿圈
                    if current_date_str in user_logs:
                        cx, cy = x + cell_size/2, y + cell_size/2
                        r = cell_size / 2 - 5
                        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=circle_color, width=3)
                        
                        # 可以在圈下面加个小对钩或者文字？图片里只有圈。
                        
                    # 如果是今天，额外标记一下？虽然图片里没展示
                    # if current_date_str == today_date_str:
                    #     draw.rectangle(shape_box, outline="#FF4500", width=2)

                col_idx += 1
            row_idx += 1
            
        return image
