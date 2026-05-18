from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from datetime import datetime

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)

# Color scheme
DARK_BG = RGBColor(0x1A, 0x1A, 0x2E)
ACCENT = RGBColor(0xE9, 0x45, 0x60)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_GRAY = RGBColor(0xCC, 0xCC, 0xCC)
CARD_BG = RGBColor(0x16, 0x21, 0x3E)

def add_bg(slide, color=DARK_BG):
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color

def add_text_box(slide, left, top, width, height, text, font_size=18, color=WHITE, bold=False, alignment=PP_ALIGN.LEFT):
    txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = Pt(font_size)
    p.font.color.rgb = color
    p.font.bold = bold
    p.alignment = alignment
    return tf

# ── Slide 1: Title ──
slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
add_bg(slide)
add_text_box(slide, 1, 1.5, 11.3, 1.5, "新浪新闻速览", font_size=48, color=ACCENT, bold=True, alignment=PP_ALIGN.CENTER)
add_text_box(slide, 1, 3.2, 11.3, 1, "Sina News Highlights", font_size=28, color=LIGHT_GRAY, alignment=PP_ALIGN.CENTER)
add_text_box(slide, 1, 4.5, 11.3, 0.8, "2026年5月18日", font_size=20, color=LIGHT_GRAY, alignment=PP_ALIGN.CENTER)

# ── Slide 2: 要闻 Top Headlines (1-8) ──
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_bg(slide)
add_text_box(slide, 0.5, 0.3, 12, 0.8, "📰 要闻 Top Headlines", font_size=32, color=ACCENT, bold=True)

headlines_1 = [
    '1. 历史性的一页，要用历史的长镜头去端详',
    '2. 让收藏在博物馆里的文物活起来 心相近 文脉华章',
    '3. 普京来华，看点不少',
    '4. 中东又开始新一轮倒计时了',
    '5. 美总统涉台\u201c四不\u201d示警\u201c台独\u201d',
    '6. 高市早苗的战略焦虑 被一通电话彻底暴露',
    '7. 欧盟\u201c抄家式\u201d调查中企 中国政府下场了',
    '8. 柳州地震最后1名被困人员获救：系91岁老人',
]
tf = add_text_box(slide, 0.8, 1.3, 11.5, 5.5, "", font_size=20, color=WHITE)
for i, h in enumerate(headlines_1):
    if i == 0:
        p = tf.paragraphs[0]
    else:
        p = tf.add_paragraph()
    p.text = h
    p.font.size = Pt(20)
    p.font.color.rgb = WHITE
    p.space_after = Pt(10)

# ── Slide 3: 要闻 Top Headlines (9-15) ──
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_bg(slide)
add_text_box(slide, 0.5, 0.3, 12, 0.8, "📰 要闻 Top Headlines (续)", font_size=32, color=ACCENT, bold=True)

headlines_2 = [
    '9. 特朗普警告伊朗迅速行动否则一无所有',
    '10. 武汉紧急通知：全市中小学、幼儿园暂停户外教学活动',
    '11. 广西环江过桥车辆坠河事件已致2人死亡 仍有8人失联',
    '12. 国家统计局：1-4月全国城镇调查失业率平均值为5.3%',
    '13. 俄副外长：将继续支持古巴应对美制裁压力',
    '14. A股又迎新股王',
    '15. \u201c病了\u201d的长江，现在变样了',
]
tf = add_text_box(slide, 0.8, 1.3, 11.5, 5.5, "", font_size=20, color=WHITE)
for i, h in enumerate(headlines_2):
    if i == 0:
        p = tf.paragraphs[0]
    else:
        p = tf.add_paragraph()
    p.text = h
    p.font.size = Pt(20)
    p.font.color.rgb = WHITE
    p.space_after = Pt(10)

# ── Slide 4: 热榜 Trending ──
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_bg(slide)
add_text_box(slide, 0.5, 0.3, 12, 0.8, "🔥 热榜 Trending", font_size=32, color=ACCENT, bold=True)

trending = [
    "🔥 樊振东回应获欧冠冠军",
    "🌧️ 本轮大范围降雨到哪了",
    "✈️ 美国2战机飞行表演时撞毁",
    "🎤 网传歌手袭榜改成大魔王了",
    "🚗 小米YU7GT发布会定档",
    "🏀 骑士vs活塞",
    "🌍 马克龙非洲行翻车",
    "⚠️ 襄阳特大暴雨致道路及车辆被淹？网警辟谣",
    "💰 他应聘被嘲笑如今年赚千万",
    "🤿 马尔代夫发生史上最严重单次潜水事故",
]
tf = add_text_box(slide, 0.8, 1.3, 11.5, 5.5, "", font_size=20, color=WHITE)
for i, h in enumerate(trending):
    if i == 0:
        p = tf.paragraphs[0]
    else:
        p = tf.add_paragraph()
    p.text = h
    p.font.size = Pt(20)
    p.font.color.rgb = WHITE
    p.space_after = Pt(10)

# ── Slide 5: 国际 International ──
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_bg(slide)
add_text_box(slide, 0.5, 0.3, 12, 0.8, "🌍 国际 International", font_size=32, color=ACCENT, bold=True)

intl = [
    "🇸🇪 瑞典：印度总理莫迪访问瑞典",
    "🇵🇸 加沙地带：以色列空袭造成人员死亡",
    "🇺🇦 俄乌冲突：前线局势持续紧张",
    "🇺🇸 美国国内政治动态",
    "🇪🇺 欧盟对华政策调整",
]
tf = add_text_box(slide, 0.8, 1.3, 11.5, 5.5, "", font_size=22, color=WHITE)
for i, h in enumerate(intl):
    if i == 0:
        p = tf.paragraphs[0]
    else:
        p = tf.add_paragraph()
    p.text = h
    p.font.size = Pt(22)
    p.font.color.rgb = WHITE
    p.space_after = Pt(14)

# ── Slide 6: Closing ──
slide = prs.slides.add_slide(prs.slide_layouts[6])
add_bg(slide)
add_text_box(slide, 1, 2.5, 11.3, 1.5, "谢谢观看", font_size=48, color=ACCENT, bold=True, alignment=PP_ALIGN.CENTER)
add_text_box(slide, 1, 4.2, 11.3, 1, "Thank You", font_size=28, color=LIGHT_GRAY, alignment=PP_ALIGN.CENTER)
add_text_box(slide, 1, 5.2, 11.3, 0.8, "数据来源：新浪新闻 sina.com.cn", font_size=16, color=LIGHT_GRAY, alignment=PP_ALIGN.CENTER)

output_path = "sina_news_20260518.pptx"
prs.save(output_path)
print(f"Saved to {output_path}")
