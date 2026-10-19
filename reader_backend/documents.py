from pathlib import Path
from html.parser import HTMLParser
from zipfile import ZipFile
from io import BytesIO
import hashlib
import re
import textwrap
import xml.etree.ElementTree as ET
import posixpath
import pymupdf
from .storage import Store


def recover_cid(text):
    punctuation={'\u01c2':'　','\u01c3':'、','\u01c4':'。','\u01c9':'《','\u01ca':'》'}
    def convert(c):
        if c in punctuation:return punctuation[c]
        if c in '—–“”‘’…':return c
        n=ord(c)
        if 0x430<=n<=0x562f:return chr(n+0x49d0)
        if 0x2bd<=n<=0x31a:return chr(n+0xfc44)
        return c
    return ''.join(map(convert,text))


def pdf_page_text(page):
    blocks=page.get_text('dict')['blocks']
    fonts={}
    for block in blocks:
        for line in block.get('lines',[]):
            for span in line['spans']:fonts[span['font']]=fonts.get(span['font'],'')+span['text']
    broken={font for font,text in fonts.items() if font=='font000000002156afc2' or '䱯ቄ⌅' in text or ('䟿ॆ' in text and 'Ⲵ' in text)}
    paragraphs=[]
    for block in blocks:
        lines=[]
        for line in block.get('lines',[]):
            lines.append(''.join(recover_cid(span['text']) if span['font'] in broken else span['text'] for span in line['spans']))
        if lines:paragraphs.append('\n'.join(lines))
    return '\n\n'.join(paragraphs)


class HTMLText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.ignored=0;self.has_image=False
    def handle_starttag(self,tag,attrs):
        if tag=='img':self.has_image=True
        if tag in ('script','style','head'):self.ignored+=1
        if tag in ('p','h1','h2','h3','li','br'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','head'):self.ignored=max(0,self.ignored-1)
    def handle_data(self,text):
        if not self.ignored:self.parts.append(text)


class Documents:
    def __init__(self, store: Store):self.store=store

    def ingest(self, data: bytes, name: str):
        if len(data)>20*1024*1024:raise ValueError('书籍不能超过20MB')
        kind=Path(name).suffix.lower().lstrip('.')
        if kind not in ('pdf','epub','txt'):raise ValueError('只支持PDF、EPUB、TXT')
        identifier=hashlib.sha256(data).hexdigest()
        try:return self.store.book(identifier)
        except KeyError:pass
        directory=self.store.directory/'books';directory.mkdir(exist_ok=True)
        path=directory/f'{identifier}.{kind}';path.write_bytes(data)
        if kind=='pdf':
            with pymupdf.open(stream=data,filetype='pdf') as pdf:
                if pdf.needs_pass:raise ValueError('暂不支持加密PDF')
                pages=[{'text':pdf_page_text(page)} for page in pdf]
        elif kind=='txt':
            text=data.decode('utf-8-sig').replace('\r\n','\n').replace('\r','\n')
            paragraphs=[p for p in re.split(r'\n\s*\n',text) if p.strip()]
            groups=[];group=[];length=0
            for paragraph in paragraphs:
                units=len(paragraph.encode('utf-16-le'))//2
                if length+units>3000 and group:
                    groups.append(group);group=[];length=0
                group.append(paragraph);length+=units
            if group:groups.append(group)
            pages=[{'text':'\n\n'.join(group)} for group in groups]
        else:
            pages=[]
            with ZipFile(BytesIO(data)) as archive:
                entries=archive.infolist()
                if sum(entry.file_size for entry in entries)>60_000_000:raise ValueError('EPUB解压体积过大')
                container=ET.fromstring(archive.read('META-INF/container.xml'))
                package=next(node.attrib['full-path'] for node in container.iter() if node.tag.endswith('rootfile'))
                opf=ET.fromstring(archive.read(package));base=posixpath.dirname(package)
                manifest={node.attrib['id']:node.attrib for node in opf.iter() if node.tag.endswith('}item')}
                for ref in (node for node in opf.iter() if node.tag.endswith('}itemref')):
                    item=manifest[ref.attrib['idref']]
                    if 'html' not in item.get('media-type','') or 'nav' in item.get('properties','').split():continue
                    html=archive.read(posixpath.normpath(posixpath.join(base,item['href']))).decode('utf-8')
                    parser=HTMLText();parser.feed(html)
                    text=''.join(parser.parts).strip()
                    if text or parser.has_image:pages.append({'text':text,'html':html})
        if not pages:raise ValueError('书籍没有可阅读页面')
        metadata={'id':identifier,'title':Path(name).stem,'kind':kind,'path':str(path),'page_count':len(pages)}
        self.store.save_book(metadata,pages);return metadata

    def image(self, book_id, number):
        metadata=self.store.book(book_id)
        if metadata['kind']=='pdf':
            with pymupdf.open(metadata['path']) as pdf:
                page=pdf[number-1];scale=min(1.6,1200/page.rect.width)
                return page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False).tobytes('png')
        text=self.store.page(book_id,number)['text']
        lines=[]
        for paragraph in text.splitlines():lines.extend(textwrap.wrap(paragraph,48) or [''])
        # EPUB has no fixed page: label the image as a reflow preview and keep all text.
        with pymupdf.open() as document:
            page=document.new_page(width=850,height=min(12000,max(800,len(lines)*22+100)))
            page.insert_text((35,35),'EPUB / TXT reflow preview',fontsize=12)
            page.insert_text((35,75),'\n'.join(lines),fontname='china-s',fontsize=15)
            return page.get_pixmap(matrix=pymupdf.Matrix(1,1),alpha=False).tobytes('png')

    def layout(self, book_id, number):
        metadata=self.store.book(book_id)
        if metadata['kind']!='pdf':
            return {'fixed_layout':False,'note':'EPUB/TXT没有固定页面坐标；以章节文字与重排图像为准'}
        with pymupdf.open(metadata['path']) as pdf:
            page=pdf[number-1];spans=[]
            for block in page.get_text('dict')['blocks']:
                for line in block.get('lines',[]):
                    for span in line['spans']:
                        text=recover_cid(span['text']) if span['font']=='font000000002156afc2' else span['text']
                        x0,y0,x1,y1=span['bbox']
                        spans.append({'text':text,'bbox':[round(x0/page.rect.width,5),round(y0/page.rect.height,5),round(x1/page.rect.width,5),round(y1/page.rect.height,5)],
                                      'font':span['font'],'size':round(span['size'],2),'flags':span['flags'],'direction':line['dir']})
            return {'page':number,'fixed_layout':True,'coordinates':'normalized x0,y0,x1,y1; top-left origin',
                    'warning':'字符顺序不是LaTeX；上下标、分式、矩阵必须对照整页图像。无法确认时不要据此推理。',
                    'spans':spans[:2000],'truncated':len(spans)>2000}
