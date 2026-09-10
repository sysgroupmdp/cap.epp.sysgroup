import io, os, sqlite3, uuid
from datetime import date
from pathlib import Path
import pandas as pd
import streamlit as st
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter

try:
    from streamlit_drawable_canvas import st_canvas
except Exception:
    st_canvas = None

ROOT=Path(__file__).parent
DATA=ROOT/'data'; DATA.mkdir(exist_ok=True)
DB=DATA/'capacitaciones_epp.db'
PROVINCIA='BUENOS AIRES'

st.set_page_config(page_title='S&S | Capacitaciones y EPP', page_icon='🦺', layout='wide')

CSS='''<style>
.block-container{padding-top:1rem;max-width:1200px}.ss-title{font-size:2rem;font-weight:800}.muted{color:#666}
div[data-testid="stForm"]{border:1px solid #ddd;padding:18px;border-radius:12px}
</style>'''
st.markdown(CSS, unsafe_allow_html=True)

def conn():
    c=sqlite3.connect(DB, check_same_thread=False); c.row_factory=sqlite3.Row; return c

def init_db():
    c=conn(); cur=c.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS empresas(id INTEGER PRIMARY KEY, razon_social TEXT UNIQUE, cuit TEXT, direccion TEXT, localidad TEXT, tipo TEXT DEFAULT 'HABITUAL');
    CREATE TABLE IF NOT EXISTS trabajadores(id INTEGER PRIMARY KEY, empresa_id INTEGER, apellido_nombre TEXT, dni TEXT, puesto TEXT, firma BLOB, UNIQUE(empresa_id,dni));
    CREATE TABLE IF NOT EXISTS capacitaciones(id INTEGER PRIMARY KEY, empresa_id INTEGER, fecha TEXT, tematicas TEXT, visado BLOB, visado_nombre TEXT, pdf BLOB);
    CREATE TABLE IF NOT EXISTS capacitacion_asistentes(capacitacion_id INTEGER, trabajador_id INTEGER);
    CREATE TABLE IF NOT EXISTS entregas_epp(id INTEGER PRIMARY KEY, empresa_id INTEGER, trabajador_id INTEGER, fecha TEXT, tarea TEXT, info TEXT, pdf BLOB);
    CREATE TABLE IF NOT EXISTS entrega_items(id INTEGER PRIMARY KEY, entrega_id INTEGER, producto TEXT, tipo_modelo TEXT, marca TEXT, certificado TEXT, cantidad TEXT);
    '''); c.commit()
    csv=DATA/'clientes.csv'
    if csv.exists():
        try:
            for _,r in pd.read_csv(csv).fillna('').iterrows():
                if str(r.get('razon_social','')).strip():
                    cur.execute('INSERT OR IGNORE INTO empresas(razon_social,cuit,direccion,localidad,tipo) VALUES(?,?,?,?,?)',(r.razon_social,r.get('cuit',''),r.get('direccion',''),r.get('localidad',''),r.get('tipo','HABITUAL')))
            c.commit()
        except Exception: pass
    c.close()
init_db()

def q(sql,args=()):
    c=conn(); rows=c.execute(sql,args).fetchall(); c.close(); return rows

def exec1(sql,args=()):
    c=conn(); cur=c.cursor(); cur.execute(sql,args); c.commit(); i=cur.lastrowid; c.close(); return i

def empresa_options(): return q('SELECT * FROM empresas ORDER BY razon_social')
def worker_options(eid): return q('SELECT * FROM trabajadores WHERE empresa_id=? ORDER BY apellido_nombre',(eid,))

def _firma_desde_json(json_data, width=650, height=180):
    # No usamos cv.image_data: esa propiedad falla actualmente en Streamlit Cloud/Python 3.14.
    # Reconstruimos la firma desde los trazos vectoriales que devuelve el canvas.
    if not json_data or not json_data.get('objects'):
        return None
    from PIL import ImageDraw
    im = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(im)
    hay_trazo = False
    for obj in json_data.get('objects', []):
        if obj.get('type') != 'path':
            continue
        pts=[]
        left=float(obj.get('left',0)); top=float(obj.get('top',0))
        sx=float(obj.get('scaleX',1)); sy=float(obj.get('scaleY',1))
        for cmd in obj.get('path', []):
            if not cmd: continue
            op=cmd[0]
            nums=cmd[1:]
            if op in ('M','L') and len(nums)>=2:
                pts.append((left+float(nums[0])*sx, top+float(nums[1])*sy))
            elif op=='Q' and len(nums)>=4:
                pts.append((left+float(nums[2])*sx, top+float(nums[3])*sy))
            elif op=='C' and len(nums)>=6:
                pts.append((left+float(nums[4])*sx, top+float(nums[5])*sy))
        if len(pts)>=2:
            draw.line(pts, fill='black', width=max(2,int(float(obj.get('strokeWidth',3)))))
            hay_trazo=True
    if not hay_trazo:
        return None
    b=io.BytesIO(); im.save(b,format='PNG'); return b.getvalue()

def firma_widget(key):
    st.caption('Firmá dentro del recuadro con el dedo o mouse. La firma se guarda al generar el registro.')
    if st_canvas:
        cv=st_canvas(fill_color='rgba(255,255,255,0)', stroke_width=3, stroke_color='#000000', background_color='#FFFFFF', height=180, width=650, drawing_mode='freedraw', key=key)
        try:
            return _firma_desde_json(cv.json_data,650,180)
        except Exception as e:
            st.warning(f'No pude procesar la firma todavía: {e}')
            return None
    st.warning('El componente de firma no está disponible. Revisá requirements.txt.')
    return None

def empresa_block(prefix):
    empresas=empresa_options(); labels=['➕ Cliente ocasional / nuevo']+[r['razon_social'] for r in empresas]
    sel=st.selectbox('Empresa',labels,key=prefix+'emp')
    if sel==labels[0]:
        c1,c2=st.columns(2); razon=c1.text_input('Razón social *',key=prefix+'rs'); cuit=c2.text_input('CUIT (opcional)',key=prefix+'cuit')
        c3,c4=st.columns(2); direccion=c3.text_input('Dirección / domicilio (opcional)',key=prefix+'dir'); localidad=c4.text_input('Localidad',value='MAR DEL PLATA',key=prefix+'loc')
        habitual=st.checkbox('Guardar como empresa habitual',key=prefix+'hab')
        return None, {'razon_social':razon,'cuit':cuit,'direccion':direccion,'localidad':localidad,'tipo':'HABITUAL' if habitual else 'OCASIONAL'}
    r=next(x for x in empresas if x['razon_social']==sel); st.caption(f"CUIT: {r['cuit'] or '—'} · {r['direccion'] or '—'} · {r['localidad'] or '—'} · {PROVINCIA}")
    return r['id'], dict(r)

def ensure_empresa(eid,d):
    if eid:return eid
    if not d['razon_social'].strip(): raise ValueError('Falta Razón Social')
    return exec1('INSERT OR IGNORE INTO empresas(razon_social,cuit,direccion,localidad,tipo) VALUES(?,?,?,?,?)',(d['razon_social'].strip(),d['cuit'],d['direccion'],d['localidad'],d['tipo'])) or q('SELECT id FROM empresas WHERE razon_social=?',(d['razon_social'].strip(),))[0]['id']

def _firma_profesional(nombre):
    fn = 'firma_martin.png' if nombre.startswith('Martín') else 'firma_juan_ignacio.png'
    path = ROOT/'assets'/fn
    return path.read_bytes() if path.exists() else None

def _overlay_cap(empresa, fecha, temas, asistentes, instructor):
    """Crea SOLO los datos variables. El diseño sale del CAP.pdf original."""
    template = PdfReader(str(ROOT/'assets'/'CAP.pdf'))
    base_page = template.pages[0]
    W=float(base_page.mediabox.width); H=float(base_page.mediabox.height)
    b=io.BytesIO(); c=canvas.Canvas(b,pagesize=(W,H))
    c.setFillColorRGB(0,0,0)
    # Fecha - casillero superior derecho
    c.setFont('Helvetica',8.5); c.drawString(W-75,778,fecha)
    # Datos empresa, sobre los renglones ya existentes de la plantilla
    c.setFont('Helvetica-Bold',9.2)
    c.drawString(78,698,str(empresa.get('razon_social',''))[:72])
    c.drawString(42,676,str(empresa.get('cuit',''))[:28])
    direccion=' '.join(x for x in [str(empresa.get('direccion','')).strip(), str(empresa.get('localidad','')).strip()] if x)
    c.drawString(72,654,direccion[:78])
    # Temáticas: la plantilla tiene 6 renglones útiles. Ajustamos tamaño si son largas.
    y=608
    c.setFont('Helvetica',8.2)
    lineas=[]
    for t in temas:
        t=str(t).strip()
        if not t: continue
        # envolver sin alterar el contenido
        words=t.split(); cur=''
        for w in words:
            test=(cur+' '+w).strip()
            if len(test)>92 and cur:
                lineas.append(cur); cur=w
            else: cur=test
        if cur: lineas.append(cur)
    for line in lineas[:6]:
        c.drawString(22,y,line); y-=17.3
    # Asistentes. Posiciones calcadas de la tabla original (15 filas).
    row_top=458; row_h=21.55
    x_name=37; x_dni=242; x_puesto=330; x_firma=448
    for i,a in enumerate(asistentes[:15]):
        cy=row_top-i*row_h
        c.setFont('Helvetica',7.6)
        c.drawString(x_name,cy,str(a.get('apellido_nombre',''))[:43])
        c.drawString(x_dni,cy,str(a.get('dni',''))[:18])
        c.drawString(x_puesto,cy,str(a.get('puesto',''))[:24])
        if a.get('firma'):
            try:
                c.drawImage(ImageReader(io.BytesIO(a['firma'])),x_firma,cy-8,width=105,height=18,preserveAspectRatio=True,anchor='c',mask='auto')
            except Exception: pass
    # Firma del instructor: la leyenda y el pie SON de la plantilla; sólo estampamos la firma.
    sig=_firma_profesional(instructor)
    if sig:
        try:
            c.drawImage(ImageReader(io.BytesIO(sig)),220,103,width=160,height=34,preserveAspectRatio=True,anchor='c',mask='auto')
        except Exception: pass
    c.save(); return b.getvalue()

def pdf_cap(empresa,fecha,temas,asistentes,instructor,visado=None):
    template_path=ROOT/'assets'/'CAP.pdf'
    if not template_path.exists(): raise FileNotFoundError('Falta assets/CAP.pdf')
    tpl=PdfReader(str(template_path)); ov=PdfReader(io.BytesIO(_overlay_cap(empresa,fecha,temas,asistentes,instructor)))
    page=tpl.pages[0]; page.merge_page(ov.pages[0])
    out=PdfWriter(); out.add_page(page)
    if visado:
        try:
            for p in PdfReader(io.BytesIO(visado)).pages: out.add_page(p)
        except Exception: pass
    bb=io.BytesIO(); out.write(bb); return bb.getvalue()

def pdf_epp(empresa,trab,fecha,tarea,items,info=''):
    b=io.BytesIO();c=canvas.Canvas(b,pagesize=landscape(A4));W,H=landscape(A4);c.setFont('Helvetica-Bold',11);c.drawRightString(W-25,H-20,'Resolución 299/11, Anexo I');c.setFont('Helvetica-Bold',15);c.drawCentredString(W/2,H-40,'ENTREGA DE ROPA DE TRABAJO Y ELEMENTOS DE PROTECCIÓN PERSONAL')
    y=H-62;c.setFont('Helvetica',9); lines=[f"Razón Social: {empresa['razon_social']}     C.U.I.T.: {empresa.get('cuit','')}",f"Dirección: {empresa.get('direccion','')}     Localidad: {empresa.get('localidad','')}     Provincia: {PROVINCIA}",f"Nombre y Apellido del Trabajador: {trab['apellido_nombre']}     D.N.I.: {trab['dni']}",f"Descripción breve del puesto/tarea: {tarea}"]
    for s in lines:c.drawString(25,y,s);y-=17
    cols=[25,205,355,455,540,610,690,W-25]; heads=['Producto','Tipo / Modelo','Marca','Cert. SI/NO','Cantidad','Fecha','Firma']
    c.setFont('Helvetica-Bold',8);rh=28;c.rect(cols[0],y-rh,cols[-1]-cols[0],rh)
    for xx in cols[1:-1]:c.line(xx,y-rh,xx,y)
    for i,h in enumerate(heads):c.drawCentredString((cols[i]+cols[i+1])/2,y-17,h)
    y-=rh;c.setFont('Helvetica',7.5)
    for it in items:
        c.rect(cols[0],y-rh,cols[-1]-cols[0],rh)
        for xx in cols[1:-1]:c.line(xx,y-rh,xx,y)
        vals=[it['producto'],it['tipo_modelo'],it['marca'],it['certificado'],str(it['cantidad']),fecha]
        for i,v in enumerate(vals):c.drawString(cols[i]+3,y-17,str(v)[:28])
        if trab.get('firma'):
            try:c.drawImage(ImageReader(io.BytesIO(trab['firma'])),cols[6]+4,y-rh+3,width=cols[7]-cols[6]-8,height=rh-6,preserveAspectRatio=True,mask='auto')
            except:pass
        y-=rh
    c.setFont('Helvetica-Bold',8);c.drawString(25,max(25,y-15),'Información adicional:');c.setFont('Helvetica',8);c.drawString(120,max(25,y-15),info[:110]);c.save();return b.getvalue()

st.markdown('<div class="ss-title">S&S Group · Capacitaciones y EPP</div>',unsafe_allow_html=True)
st.caption('Registro digital de capacitaciones, firmas y entregas de EPP')
cap,epp,hist,emp=st.tabs(['📋 Nueva capacitación','🦺 Entrega de EPP','🗂️ Historial','🏢 Empresas'])

with cap:
    eid,ed=empresa_block('cap_'); fecha=st.date_input('Fecha',date.today(),key='cap_fecha')
    temas_df=pd.read_csv(DATA/'tematicas.csv'); opciones=temas_df['tematica'].tolist(); temas=st.multiselect('Temáticas brindadas',opciones,key='cap_temas'); manual=st.text_area('Otras temáticas (una por línea)',key='cap_manual')
    instructor=st.selectbox('Instructor por S&S Group',['Martín Nicolás Sirvent','Juan Ignacio Sirvent'],key='cap_instructor')
    st.subheader('Asistentes')
    n=st.number_input('Cantidad de asistentes a cargar',1,30,1,key='nasis')
    asistentes=[]
    for i in range(int(n)):
        with st.expander(f'Asistente {i+1}',expanded=True):
            c1,c2,c3=st.columns([2,1,1.5]); nom=c1.text_input('APELLIDO Y NOMBRE',key=f'n{i}');dni=c2.text_input('DNI / Legajo',key=f'd{i}');puesto=c3.text_input('Puesto',key=f'p{i}');firma=firma_widget(f'f{i}');asistentes.append({'apellido_nombre':nom,'dni':dni,'puesto':puesto,'firma':firma})
    vis=st.file_uploader('Visado de capacitación (PDF, opcional)',type=['pdf'])
    if st.button('💾 Guardar y generar constancia',type='primary'):
        try:
            eid=ensure_empresa(eid,ed); empd=dict(q('SELECT * FROM empresas WHERE id=?',(eid,))[0]); final=[]
            for a in asistentes:
                if not a['apellido_nombre'].strip():continue
                old=q('SELECT * FROM trabajadores WHERE empresa_id=? AND dni=?',(eid,a['dni'])) if a['dni'] else []
                if old:
                    wid=old[0]['id']; exec1('UPDATE trabajadores SET apellido_nombre=?,puesto=?,firma=COALESCE(?,firma) WHERE id=?',(a['apellido_nombre'],a['puesto'],a['firma'],wid))
                else:wid=exec1('INSERT INTO trabajadores(empresa_id,apellido_nombre,dni,puesto,firma) VALUES(?,?,?,?,?)',(eid,a['apellido_nombre'],a['dni'],a['puesto'],a['firma']))
                aa=dict(a);aa['id']=wid;final.append(aa)
            alltem=temas+[x.strip() for x in manual.splitlines() if x.strip()];vb=vis.getvalue() if vis else None;pdf=pdf_cap(empd,str(fecha.strftime('%d/%m/%Y')),alltem,final,instructor,vb)
            cid=exec1('INSERT INTO capacitaciones(empresa_id,fecha,tematicas,visado,visado_nombre,pdf) VALUES(?,?,?,?,?,?)',(eid,str(fecha),'; '.join(alltem),vb,vis.name if vis else '',pdf))
            for a in final:exec1('INSERT INTO capacitacion_asistentes(capacitacion_id,trabajador_id) VALUES(?,?)',(cid,a['id']))
            st.success('Capacitación guardada.');st.download_button('⬇️ Descargar constancia PDF',pdf,f'capacitacion_{fecha}.pdf','application/pdf')
        except Exception as e:st.error(str(e))

with epp:
    eid,ed=empresa_block('epp_'); eid_effective=eid
    if eid_effective:
        ws=worker_options(eid_effective); names=['➕ Nuevo trabajador']+[w['apellido_nombre'] for w in ws]; sn=st.selectbox('Trabajador',names)
    else: ws=[];sn='➕ Nuevo trabajador'
    if sn=='➕ Nuevo trabajador':
        c1,c2=st.columns(2);wn=c1.text_input('Apellido y nombre');wd=c2.text_input('DNI');wp=st.text_input('Puesto');wf=firma_widget('epp_new_firma');trab={'apellido_nombre':wn,'dni':wd,'puesto':wp,'firma':wf}
    else: trab=dict(next(w for w in ws if w['apellido_nombre']==sn));st.caption(f"DNI {trab['dni']} · {trab['puesto']} · se reutilizará la firma guardada")
    tareas=['Albañil','Hormigón armado','Pintura','Trabajo en altura','Excavación','Otro']; tarea=st.selectbox('Tarea / puesto para sugerir EPP',tareas); otro=st.text_input('Otro puesto/tarea') if tarea=='Otro' else ''
    mapa=pd.read_csv(DATA/'epp_por_tarea.csv'); suger=mapa[mapa.tarea==tarea].epp.tolist() if tarea!='Otro' else []; eleg=st.multiselect('EPP necesarios / entregados',sorted(set(mapa.epp.tolist())),default=suger); extra=st.text_input('Agregar otro EPP manualmente')
    productos=eleg+([extra.strip()] if extra.strip() else []); rows=[]
    st.subheader('Detalle de entrega')
    for i,p in enumerate(productos):
        c1,c2,c3,c4,c5=st.columns([2,1.5,1.2,1,0.7]);c1.write(p);tm=c2.text_input('Tipo/Modelo',key=f'tm{i}');ma=c3.text_input('Marca',key=f'ma{i}');ce=c4.selectbox('Cert.', ['SI','NO'],key=f'ce{i}');ca=c5.number_input('Cant.',1,99,1,key=f'ca{i}');rows.append({'producto':p,'tipo_modelo':tm,'marca':ma,'certificado':ce,'cantidad':ca})
    fe=st.date_input('Fecha de entrega',date.today(),key='efecha');info=st.text_area('Información adicional (opcional)')
    if st.button('💾 Guardar entrega y generar Res. 299/11',type='primary'):
        try:
            eid2=ensure_empresa(eid,ed); empd=dict(q('SELECT * FROM empresas WHERE id=?',(eid2,))[0])
            if sn=='➕ Nuevo trabajador':
                if not trab['apellido_nombre'].strip():raise ValueError('Falta nombre del trabajador')
                wid=exec1('INSERT OR REPLACE INTO trabajadores(id,empresa_id,apellido_nombre,dni,puesto,firma) VALUES((SELECT id FROM trabajadores WHERE empresa_id=? AND dni=?),?,?,?,?,?)',(eid2,trab['dni'],eid2,trab['apellido_nombre'],trab['dni'],trab['puesto'],trab['firma']))
                tr=dict(q('SELECT * FROM trabajadores WHERE empresa_id=? AND dni=?',(eid2,trab['dni']))[0]) if trab['dni'] else trab; wid=tr.get('id',wid)
            else:wid=trab['id'];tr=trab
            tt=otro.strip() if tarea=='Otro' else tarea;pdf=pdf_epp(empd,tr,str(fe.strftime('%d/%m/%Y')),tt,rows,info);de=exec1('INSERT INTO entregas_epp(empresa_id,trabajador_id,fecha,tarea,info,pdf) VALUES(?,?,?,?,?,?)',(eid2,wid,str(fe),tt,info,pdf))
            for r in rows:exec1('INSERT INTO entrega_items(entrega_id,producto,tipo_modelo,marca,certificado,cantidad) VALUES(?,?,?,?,?,?)',(de,r['producto'],r['tipo_modelo'],r['marca'],r['certificado'],str(r['cantidad'])))
            st.success('Entrega de EPP guardada.');st.download_button('⬇️ Descargar registro EPP PDF',pdf,f'EPP_{tr["apellido_nombre"]}_{fe}.pdf','application/pdf')
        except Exception as e:st.error(str(e))

with hist:
    st.subheader('Historial')
    caps=q('SELECT c.*,e.razon_social FROM capacitaciones c JOIN empresas e ON e.id=c.empresa_id ORDER BY c.fecha DESC,c.id DESC')
    eps=q('SELECT x.*,e.razon_social,t.apellido_nombre FROM entregas_epp x JOIN empresas e ON e.id=x.empresa_id JOIN trabajadores t ON t.id=x.trabajador_id ORDER BY x.fecha DESC,x.id DESC')
    st.markdown('#### Capacitaciones')
    for r in caps:
        with st.expander(f"{r['fecha']} · {r['razon_social']} · {r['tematicas']}"):
            st.download_button('Descargar PDF',r['pdf'],f"capacitacion_{r['id']}.pdf",'application/pdf',key=f"hc{r['id']}")
    st.markdown('#### Entregas EPP')
    for r in eps:
        with st.expander(f"{r['fecha']} · {r['razon_social']} · {r['apellido_nombre']} · {r['tarea']}"):
            st.download_button('Descargar PDF',r['pdf'],f"epp_{r['id']}.pdf",'application/pdf',key=f"he{r['id']}")

with emp:
    st.subheader('Empresas')
    df=pd.DataFrame([dict(x) for x in empresa_options()]);st.dataframe(df,use_container_width=True,hide_index=True)
    st.caption('Base inicial sincronizada con Gestión Administrativa: 44 clientes migrados. Los clientes ocasionales de capacitación/EPP se mantienen separados.')

    st.markdown('---')
    st.subheader('Firmas de instructores')
    st.caption('Juan Ignacio ya tiene una firma inicial recuperada de documentación previa. Podés reemplazar cualquiera de las dos por un PNG/JPG recortado de la firma.')
    for nombre,fn in [('Martín Nicolás Sirvent','firma_martin.png'),('Juan Ignacio Sirvent','firma_juan_ignacio.png')]:
        path=ROOT/'assets'/fn
        c1,c2=st.columns([2,1])
        with c1:
            st.write('**'+nombre+'**')
            if path.exists():
                st.image(str(path),width=260)
            else:
                st.warning('Firma todavía no cargada.')
        with c2:
            up=st.file_uploader('Cargar/reemplazar firma',type=['png','jpg','jpeg'],key='sig_'+fn)
            if up is not None and st.button('Guardar firma',key='save_'+fn):
                im=Image.open(up).convert('RGBA'); im.thumbnail((1400,500)); im.save(path,'PNG'); st.success('Firma guardada.'); st.rerun()

