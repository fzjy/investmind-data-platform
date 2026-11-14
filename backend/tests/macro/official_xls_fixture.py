"""最小BIFF8字节夹具；不安装写表依赖，不请求官方站点。"""
import struct

HEADERS=['日期\nDate','指数代码\nIndex Code','指数名称\nIndex Name','指数英文名称\nIndex Name(Eng)',
    '成份券代码\nConstituent Code','成份券名称\nConstituent Name','成份券英文名称\nConstituent Name(Eng)',
    '交易所\nExchange','交易所英文名称\nExchange(Eng)']


def workbook(rows=None,headers=None):
    rows=rows if rows is not None else [
        ['20261023','000300','沪深300','CSI 300','600000','浦发银行','SPD BANK','上海证券交易所','Shanghai Stock Exchange'],
        ['20261023','000300','沪深300','CSI 300','000001','平安银行','PAB','深圳证券交易所','Shenzhen Stock Exchange']]
    records=[headers if headers is not None else HEADERS,*rows]
    def record(key,data=b''):return struct.pack('<HH',key,len(data))+data
    def bof(kind):return record(0x0809,struct.pack('<HHHHII',0x0600,kind,3515,1997,0xc0,6))
    def bound(offset):return record(0x0085,struct.pack('<IBBBB',offset,0,0,7,0)+b'Members')
    globals=bof(5)+record(0x0042,struct.pack('<H',1200))
    offset=len(globals)+len(bound(0))+len(record(0x000a))
    content=globals+bound(offset)+record(0x000a)
    content+=bof(0x10)+record(0x0200,struct.pack('<IIHHH',0,len(records),0,9,0))
    for r,row in enumerate(records):
        for c,value in enumerate(row):
            if type(value) is bool:content+=record(0x0205,struct.pack('<HHHBB',r,c,0,int(value),0))
            elif type(value) in (int,float):content+=record(0x0203,struct.pack('<HHHd',r,c,0,value))
            else:content+=record(0x0204,struct.pack('<HHHHB',r,c,0,len(value),1)+value.encode('utf-16-le'))
    return content+record(0x000a)


def compound_workbook(rows=None):
    """最小OLE复合容器，Workbook普通流，不含宏/外链。"""
    data=workbook(rows);size=max(4096,(len(data)+511)//512*512);sectors=size//512
    assert sectors+2<=128
    free=0xffffffff;end=0xfffffffe
    header=bytearray(512);header[:8]=b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    struct.pack_into('<HHHHH',header,24,0x3e,3,0xfffe,9,6)
    struct.pack_into('<IIIIIIIII',header,40,0,1,0,0,4096,end,0,end,0)
    struct.pack_into('<109I',header,76,1,*([free]*108))
    def entry(name,kind,child,start,length):
        value=bytearray(128);encoded=(name+'\0').encode('utf-16-le');value[:len(encoded)]=encoded
        struct.pack_into('<HBBIII',value,64,len(encoded),kind,1,free,free,child)
        struct.pack_into('<IQ',value,116,start,length);return value
    directory=entry('Root Entry',5,1,end,0)+entry('Workbook',2,free,2,size)+bytearray(256)
    fat=[end,0xfffffffd,*range(3,sectors+2),end]
    return bytes(header+directory+struct.pack('<128I',*fat,*([free]*(128-len(fat))))+data.ljust(size,b'\0'))
