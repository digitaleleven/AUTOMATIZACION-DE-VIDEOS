import { useState } from 'react';
import {
  Activity,
  ArrowDownRight,
  ArrowRight,
  AudioLines,
  ChevronDown,
  CircleHelp,
  Clapperboard,
  Clock3,
  CloudOff,
  Film,
  FolderOpen,
  HardDrive,
  ImagePlus,
  Layers3,
  LockKeyhole,
  MonitorPlay,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Upload,
  Video,
  WandSparkles,
} from 'lucide-react';

const navItems = [
  { id: 'inicio', label: 'Inicio', icon: Layers3 },
  { id: 'biblioteca', label: 'Biblioteca', icon: FolderOpen },
  { id: 'estudio', label: 'Estudio de edición', icon: Clapperboard },
  { id: 'procesamiento', label: 'Procesamiento', icon: Activity },
  { id: 'configuracion', label: 'Configuración', icon: Settings2 },
];

const styleFields = [
  { label: 'Familia tipográfica', name: 'font_family', type: 'text', placeholder: 'Sin consultar' },
  { label: 'Tamaño de fuente', name: 'font_size', type: 'number', placeholder: '—' },
  { label: 'Grosor de trazo', name: 'stroke_width', type: 'number', placeholder: '—' },
  { label: 'Color de texto', name: 'font_color', type: 'color' },
  { label: 'Color de trazo', name: 'stroke_color', type: 'color' },
  { label: 'Color de resaltado', name: 'highlight_color', type: 'color' },
  { label: 'Estilo de animación', name: 'animation_style', type: 'select', options: ['Sin consultar', 'Resaltar palabra', 'Karaoke'] },
  { label: 'Posición de subtítulos', name: 'caption_position', type: 'select', options: ['Sin consultar', 'Inferior', 'Centro', 'Superior'] },
  { label: 'Desplazamiento vertical', name: 'caption_offset_y', type: 'number', placeholder: '—' },
  { label: 'Ruta local del logo', name: 'logo_path', type: 'text', placeholder: 'Sin logo configurado' },
  { label: 'FPS de salida', name: 'output_fps', type: 'number', placeholder: '—' },
  { label: 'Tamaño máximo de entrada (MB)', name: 'max_input_size_mb', type: 'number', placeholder: '—' },
  { label: 'Duración máxima (segundos)', name: 'max_video_duration_seconds', type: 'number', placeholder: '—' },
  { label: 'Máximo de bloques de transcripción', name: 'max_transcription_chunks', type: 'number', placeholder: '—' },
  { label: 'OpenAI · USD / minuto de audio', name: 'openai_price_per_audio_minute_usd', type: 'number', placeholder: 'Desconocido' },
  { label: 'Shotstack · USD / minuto de render', name: 'shotstack_price_per_render_minute_usd', type: 'number', placeholder: 'Desconocido' },
  { label: 'Límite estimado total (USD)', name: 'max_estimated_cost_usd', type: 'number', placeholder: 'Sin límite consultado' },
] as const;

function PreviewPane({ output = false }: { output?: boolean }) {
  return (
    <div className="relative flex min-h-[244px] flex-col items-center justify-center overflow-hidden rounded-[18px] border border-[#d8ded6] bg-[#e9eee8] p-5 text-center">
      <div aria-hidden="true" className="absolute inset-0 opacity-45" style={{ backgroundImage: 'linear-gradient(rgba(77,102,86,.08) 1px, transparent 1px), linear-gradient(90deg, rgba(77,102,86,.08) 1px, transparent 1px)', backgroundSize: '26px 26px' }} />
      <div className="relative flex h-12 w-12 items-center justify-center rounded-2xl border border-[#c7d1c7] bg-[#f4f6f1] text-[#58705f]">
        {output ? <MonitorPlay size={20} strokeWidth={1.7} /> : <Video size={20} strokeWidth={1.7} />}
      </div>
      <p className="relative mt-4 text-[13px] font-semibold text-[#26372e]">{output ? 'Aún no hay resultado' : 'Selecciona un original'}</p>
      <p className="relative mt-1.5 max-w-[235px] text-[11px] leading-5 text-[#738076]">
        {output ? 'El archivo vertical nuevo aparecerá aquí cuando exista.' : 'La vista del archivo fuente y sus metadatos aparecerán aquí.'}
      </p>
      <span className="absolute bottom-3 right-3 rounded-md border border-[#d4dbd2] bg-[#f5f7f2] px-2 py-1 font-mono text-[9px] tracking-[.12em] text-[#68776b]">
        {output ? 'SALIDA · 9:16' : 'ORIGINAL · FUENTE'}
      </span>
    </div>
  );
}

function Field({ field }: { field: (typeof styleFields)[number] }) {
  return (
    <label className="block min-w-0 text-[11px] font-medium text-[#506057]">
      {field.label}
      {field.type === 'select' ? (
        <span className="relative mt-1.5 block">
          <select aria-label={field.label} defaultValue="Sin consultar" className="h-10 w-full appearance-none rounded-lg border border-[#d9dfd8] bg-[#f9faf6] px-3 pr-8 text-[11px] text-[#7c877e] outline-none focus:border-[#466a50] focus:ring-2 focus:ring-[#466a50]/15">
            {field.options?.map((option) => <option key={option}>{option}</option>)}
          </select>
          <ChevronDown aria-hidden="true" size={14} className="pointer-events-none absolute right-3 top-3 text-[#7b877e]" />
        </span>
      ) : (
        <input
          aria-label={field.label}
          type={field.type}
          placeholder={field.type === 'color' ? undefined : field.placeholder}
          className={`mt-1.5 h-10 w-full rounded-lg border border-[#d9dfd8] bg-[#f9faf6] px-3 text-[11px] text-[#26372e] outline-none placeholder:text-[#a0aaa0] focus:border-[#466a50] focus:ring-2 focus:ring-[#466a50]/15 ${field.type === 'color' ? 'cursor-pointer p-1.5' : ''}`}
        />
      )}
    </label>
  );
}

export function Premium() {
  const [active, setActive] = useState('inicio');
  const [notice, setNotice] = useState(false);
  const [consent, setConsent] = useState(false);
  const [unknownConsent, setUnknownConsent] = useState(false);

  const navigate = (id: string) => {
    setActive(id);
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className="min-h-[100dvh] bg-[#f2f4ef] text-[#202d25] [font-family:ui-sans-serif,system-ui,sans-serif]">
      <style>{`
        * { box-sizing: border-box; }
        html { scroll-behavior: smooth; }
        .pliegue-focus:focus-visible { outline: 3px solid #d4ef69; outline-offset: 3px; }
        @media (prefers-reduced-motion: reduce) {
          *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
        }
      `}</style>
      <div className="mx-auto flex min-h-[100dvh] max-w-[1600px]">
        <aside className="sticky top-0 hidden h-[100dvh] w-[238px] shrink-0 flex-col border-r border-[#dfe4dc] bg-[#e9ede6] px-5 py-6 md:flex">
          <button type="button" onClick={() => navigate('inicio')} className="pliegue-focus flex items-center gap-3 rounded-xl text-left">
            <span className="grid h-10 w-10 place-items-center rounded-[13px] bg-[#253b2d] text-[#d8ef73]">
              <span className="font-serif text-[25px] italic leading-none">p</span>
            </span>
            <span>
              <span className="block text-[17px] font-bold tracking-[-.06em]">pliegue</span>
              <span className="mt-0.5 block font-mono text-[8px] font-semibold tracking-[.19em] text-[#738074]">LOCAL EDITING DESK</span>
            </span>
          </button>
          <div className="mb-3 mt-11 px-3 font-mono text-[9px] font-semibold tracking-[.16em] text-[#89958a]">ESPACIO DE TRABAJO</div>
          <nav aria-label="Navegación principal" className="space-y-1">
            {navItems.map(({ id, label, icon: Icon }) => (
              <button key={id} type="button" onClick={() => navigate(id)} aria-current={active === id ? 'page' : undefined} className={`pliegue-focus flex w-full items-center gap-3 rounded-[11px] px-3 py-3 text-left text-[12px] font-semibold transition-colors ${active === id ? 'bg-[#dce5d8] text-[#294633]' : 'text-[#6b786d] hover:bg-[#e1e7df] hover:text-[#2d4434]'}`}>
                <Icon size={16} strokeWidth={1.8} />
                {label}
                {id === 'biblioteca' && <span className="ml-auto rounded-md border border-[#d1d9ce] px-1.5 py-0.5 font-mono text-[8px] text-[#839085]">—</span>}
              </button>
            ))}
          </nav>
          <div className="mt-auto rounded-[15px] border border-[#d9e0d5] bg-[#f1f4ed] p-4">
            <div className="flex items-center gap-2 text-[11px] font-semibold text-[#455e4b]"><HardDrive size={15} /> Entorno local</div>
            <p className="mb-0 mt-2 text-[10px] leading-[1.65] text-[#748075]">Los originales permanecen intactos. Ningún envío ocurre sin autorización explícita.</p>
            <div className="mt-3 flex items-center gap-2 border-t border-[#dfe5db] pt-3 font-mono text-[9px] text-[#879187]">
              <span className="h-1.5 w-1.5 rounded-full bg-[#b98b55]" /> Servicio sin verificar
            </div>
          </div>
        </aside>

        <main className="min-w-0 flex-1 px-4 pb-10 sm:px-6 lg:px-10 xl:px-12">
          <header className="sticky top-0 z-20 -mx-4 flex min-h-[62px] items-center justify-between border-b border-[#dce2d9] bg-[#f2f4ef]/95 px-4 backdrop-blur sm:-mx-6 sm:px-6 lg:-mx-10 lg:px-10 xl:-mx-12 xl:px-12">
            <div className="flex items-center gap-3 md:hidden">
              <span className="grid h-8 w-8 place-items-center rounded-[10px] bg-[#253b2d] font-serif text-[21px] italic text-[#d8ef73]">p</span>
              <span className="font-bold tracking-[-.05em]">pliegue</span>
            </div>
            <div className="hidden items-center gap-2 font-mono text-[9px] font-medium tracking-[.13em] text-[#89948b] md:flex">
              <span>PLIEGUE</span><span className="text-[#c0c8be]">/</span><span className="text-[#4d5c51]">MESA DE EDICIÓN</span>
            </div>
            <div className="flex items-center gap-2 rounded-full border border-[#d9e0d7] bg-[#f7f8f4] px-3 py-1.5 text-[10px] font-semibold text-[#617065]">
              <span className="h-1.5 w-1.5 rounded-full bg-[#b78c55]" /> Prototipo · sin conexión
            </div>
          </header>

          <div className="mx-auto max-w-[1260px]">
            <section id="inicio" className="scroll-mt-20 pb-7 pt-8 sm:pt-11">
              <div className="flex flex-col justify-between gap-7 xl:flex-row xl:items-end">
                <div className="max-w-[700px]">
                  <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-[#d9e1d5] bg-[#e9eee6] px-3 py-1.5 font-mono text-[9px] font-semibold tracking-[.12em] text-[#55705a]">
                    <Sparkles size={12} /> ESCRITORIO DE EDICIÓN LOCAL <span className="text-[#a1aa9e]">·</span> VERTICAL 9:16
                  </div>
                  <h1 className="text-[clamp(36px,5vw,64px)] font-semibold leading-[.99] tracking-[-.065em] text-[#24352a]">
                    Del clip a la historia<span className="text-[#799047]">.</span>
                  </h1>
                  <p className="mb-0 mt-4 max-w-[580px] text-[13px] leading-[1.75] text-[#69776b]">
                    Prepara clips de iPhone con subtítulos a tiempo. Revisa el original, el coste y cada destino antes de autorizar un procesamiento externo.
                  </p>
                </div>
                <div className="flex w-full flex-col gap-2 xl:w-[290px]">
                  <button type="button" onClick={() => setNotice(true)} className="pliegue-focus flex min-h-[52px] items-center justify-between rounded-[13px] bg-[#c9e65b] px-5 text-[13px] font-bold text-[#263827] shadow-[0_5px_0_#a9c743] transition-transform hover:-translate-y-0.5 active:translate-y-0">
                    <span className="flex items-center gap-2.5"><ImagePlus size={18} /> Añadir vídeo</span><ArrowRight size={17} />
                  </button>
                  <div className="text-center text-[9px] font-medium text-[#879186]">Vista de diseño · no carga archivos</div>
                </div>
              </div>
              <div className="mt-8 flex flex-col gap-3 rounded-[14px] border border-[#dce3d8] bg-[#e9eee6] px-4 py-3.5 sm:flex-row sm:items-center sm:px-5">
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-[#f5f7f1] text-[#58705d]"><ShieldCheck size={15} /></span>
                <p className="mb-0 flex-1 text-[11px] leading-[1.65] text-[#5c6d60]"><strong className="font-semibold text-[#34483a]">El original no se modifica.</strong> OpenAI y Shotstack solo se usarían tras una autorización explícita. Esta maqueta no se conecta a servicios ni envía medios.</p>
                <span className="shrink-0 rounded-md border border-[#d3ddd0] bg-[#f4f6f1] px-2.5 py-1.5 font-mono text-[8px] tracking-[.1em] text-[#708072]">SOLO LOCAL</span>
              </div>
            </section>

            <section id="biblioteca" className="scroll-mt-20 pb-8 pt-2">
              <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
                <div>
                  <div className="mb-1.5 font-mono text-[9px] font-semibold tracking-[.15em] text-[#849185]">01 <span className="text-[#b3beb2]">/</span> BIBLIOTECA</div>
                  <h2 className="text-[22px] font-semibold tracking-[-.045em] text-[#2a3b30]">Tu material</h2>
                </div>
                <span className="rounded-lg border border-[#dce2d9] bg-[#f7f8f4] px-2.5 py-1.5 font-mono text-[9px] text-[#788479]">SIN DATOS REALES</span>
              </div>
              <div className="grid gap-4 xl:grid-cols-[.93fr_1.07fr]">
                <div className="flex min-h-[310px] flex-col rounded-[18px] border border-[#dce2d9] bg-[#f8f9f5] p-5 sm:p-6">
                  <div className="flex items-center justify-between border-b border-[#e5e9e2] pb-4">
                    <div className="font-mono text-[9px] font-semibold tracking-[.14em] text-[#7b887d]">FUENTES DISPONIBLES</div>
                    <div className="flex items-center gap-1.5 text-[9px] font-medium text-[#8a958b]"><CloudOff size={13} /> Biblioteca sin consultar</div>
                  </div>
                  <div className="flex flex-1 flex-col items-center justify-center py-7 text-center">
                    <div className="relative mb-4 grid h-[58px] w-[58px] place-items-center rounded-[18px] border border-[#dbe3d7] bg-[#edf2e9] text-[#617962]">
                      <Film size={23} strokeWidth={1.6} />
                      <span className="absolute -bottom-1 -right-1 grid h-5 w-5 place-items-center rounded-full border-2 border-[#f8f9f5] bg-[#c9e65b] text-[#34432e]"><span className="text-[12px] leading-none">+</span></span>
                    </div>
                    <h3 className="mb-1 text-[14px] font-semibold tracking-[-.02em] text-[#334538]">Tu mesa está vacía</h3>
                    <p className="mb-4 max-w-[290px] text-[11px] leading-[1.65] text-[#7c897e]">Este canvas de diseño no contiene vídeos reales ni consulta tu biblioteca local.</p>
                    <button type="button" onClick={() => setNotice(true)} className="pliegue-focus inline-flex items-center gap-2 rounded-lg border border-[#b7c7b2] bg-[#f4f7f0] px-3.5 py-2.5 text-[10px] font-bold text-[#48634c] hover:bg-[#eaf0e4]">
                      <Upload size={13} /> Añadir vídeo <span className="font-medium text-[#879487]">· vista previa</span>
                    </button>
                  </div>
                  <div className="flex items-start gap-2 border-t border-[#e5e9e2] pt-3 text-[9px] leading-4 text-[#8a958a]">
                    <LockKeyhole size={12} className="mt-0.5 shrink-0" /> Los archivos fuente no se alteran y no se suben desde esta maqueta.
                  </div>
                </div>

                <div id="estudio" className="scroll-mt-20 rounded-[18px] border border-[#dce2d9] bg-[#f8f9f5] p-4 sm:p-5">
                  <div className="mb-3 flex items-center justify-between">
                    <div>
                      <div className="font-mono text-[9px] font-semibold tracking-[.13em] text-[#7b887d]">02 / ESTUDIO DE EDICIÓN</div>
                      <div className="mt-1 text-[13px] font-semibold text-[#34463a]">Fuente y resultado</div>
                    </div>
                    <span className="rounded-md bg-[#edf1e9] px-2 py-1 font-mono text-[8px] tracking-[.1em] text-[#7b897c]">SIN SELECCIÓN</span>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="min-w-0">
                      <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold text-[#617064]"><Video size={13} /> Original</div>
                      <PreviewPane />
                      <p className="mb-0 mt-2 text-[9px] text-[#89948a]">El archivo de origen se conserva intacto.</p>
                    </div>
                    <div className="min-w-0">
                      <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold text-[#617064]"><MonitorPlay size={13} /> Salida vertical</div>
                      <PreviewPane output />
                      <p className="mb-0 mt-2 text-[9px] text-[#89948a]">Archivo nuevo · formato vertical 9:16.</p>
                    </div>
                  </div>
                  <div className="mt-4 grid grid-cols-2 gap-2 border-t border-[#e5e9e2] pt-3 sm:grid-cols-4">
                    {[['DURACIÓN', '—'], ['TAMAÑO', '—'], ['RESOLUCIÓN', '—'], ['CÓDEC / FPS', '—']].map(([key, value]) => (
                      <div key={key}><div className="font-mono text-[8px] tracking-[.1em] text-[#9aa49a]">{key}</div><div className="mt-1 text-[10px] font-semibold text-[#69766b]">{value}</div></div>
                    ))}
                  </div>
                </div>
              </div>
            </section>

            <section id="procesamiento" className="scroll-mt-20 pb-9 pt-2">
              <div className="mb-4 flex items-end justify-between gap-3">
                <div>
                  <div className="mb-1.5 font-mono text-[9px] font-semibold tracking-[.15em] text-[#849185]">03 <span className="text-[#b3beb2]">/</span> PROCESAMIENTO</div>
                  <h2 className="text-[22px] font-semibold tracking-[-.045em] text-[#2a3b30]">Revisa antes de renderizar</h2>
                </div>
                <span className="rounded-lg border border-[#dce2d9] bg-[#f7f8f4] px-2.5 py-1.5 font-mono text-[9px] text-[#788479]">SIN PROCESOS</span>
              </div>
              <div className="grid overflow-hidden rounded-[18px] border border-[#dce2d9] bg-[#f8f9f5] lg:grid-cols-[1fr_340px]">
                <div className="p-5 sm:p-6">
                  <div className="flex items-start gap-3">
                    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[#edf1e9] text-[#647765]"><Activity size={17} /></span>
                    <div>
                      <h3 className="mb-1 text-[13px] font-semibold text-[#35473a]">Sin trabajos en esta sesión</h3>
                      <p className="mb-0 max-w-[440px] text-[10px] leading-[1.65] text-[#7b887d]">El estado, la fase y el progreso solo aparecerían al recibir datos reales del servicio. Esta maqueta no inicia trabajos.</p>
                    </div>
                  </div>
                  <div className="mt-5 rounded-xl border border-[#e3e8e0] bg-[#f3f5f0] p-4">
                    <div className="flex items-center gap-2 font-mono text-[9px] font-semibold tracking-[.12em] text-[#728075]"><CircleHelp size={13} /> ESTADO Y ERRORES</div>
                    <p className="mb-0 mt-2 text-[10px] leading-[1.7] text-[#7c897e]">No hay un trabajo que informar. Si el servicio devuelve un error real, aparecerá aquí con su mensaje; no se inventan estados ni se reintenta automáticamente.</p>
                  </div>
                </div>
                <div className="border-t border-[#e1e6de] bg-[#edf1e9] p-5 sm:p-6 lg:border-l lg:border-t-0">
                  <div className="flex items-center justify-between font-mono text-[9px] font-semibold tracking-[.12em] text-[#768477]">
                    <span>ESTIMACIÓN DE COSTE</span><span className="rounded-md border border-[#d4ddd0] bg-[#f4f6f1] px-2 py-1 text-[8px] tracking-[.08em]">SIN DATOS</span>
                  </div>
                  <div className="mt-3 text-[28px] font-semibold tracking-[-.06em] text-[#344638]">Desconocido</div>
                  <p className="mb-0 mt-1 text-[10px] leading-[1.6] text-[#778479]">No hay una estimación disponible. No se ha supuesto ningún precio.</p>
                  <div className="my-4 h-px bg-[#dce3d8]" />
                  <div className="space-y-2.5">
                    {[
                      ['OpenAI', <AudioLines size={13} key="openai" />],
                      ['Shotstack', <Clapperboard size={13} key="shotstack" />],
                      ['Entorno', <HardDrive size={13} key="env" />],
                      ['Google Drive', <FolderOpen size={13} key="drive" />],
                    ].map(([name, icon]) => (
                      <div key={name as string} className="flex items-center justify-between text-[10px] text-[#68776b]">
                        <span className="flex items-center gap-2">{icon}{name}</span>
                        <span className="font-mono text-[8px] text-[#9a7a4b]">Sin verificar</span>
                      </div>
                    ))}
                  </div>
                  <label className="mt-4 flex cursor-pointer items-start gap-2.5 border-t border-[#dce3d8] pt-4 text-[9px] leading-[1.55] text-[#69776b]">
                    <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} className="mt-0.5 h-3.5 w-3.5 accent-[#799047]" />
                    Autorizo el envío de este vídeo a servicios externos de pago.
                  </label>
                  <label className="mt-2 flex cursor-pointer items-start gap-2.5 text-[9px] leading-[1.55] text-[#69776b]">
                    <input type="checkbox" checked={unknownConsent} onChange={(e) => setUnknownConsent(e.target.checked)} className="mt-0.5 h-3.5 w-3.5 accent-[#799047]" />
                    Entiendo que el coste es desconocido y podría haber cargos.
                  </label>
                  <button type="button" disabled className="mt-4 flex w-full cursor-not-allowed items-center justify-between rounded-[10px] bg-[#c9e65b] px-4 py-3 text-[10px] font-bold text-[#35412a] opacity-55" aria-label="Iniciar procesamiento, desactivado en la vista de diseño">
                    <span>Iniciar procesamiento</span><ArrowDownRight size={15} />
                  </button>
                  <p className="mb-0 mt-2 text-center font-mono text-[8px] leading-4 text-[#899588]">DESACTIVADO · VISTA DE DISEÑO</p>
                </div>
              </div>
            </section>

            <section id="configuracion" className="scroll-mt-20 pb-7 pt-2">
              <div className="mb-4 flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
                <div>
                  <div className="mb-1.5 font-mono text-[9px] font-semibold tracking-[.15em] text-[#849185]">04 <span className="text-[#b3beb2]">/</span> CONFIGURACIÓN</div>
                  <h2 className="text-[22px] font-semibold tracking-[-.045em] text-[#2a3b30]">Estilo de subtítulos</h2>
                  <p className="mb-0 mt-1.5 text-[10px] leading-5 text-[#7b887d]">Controles del estilo de vídeo. Los valores actuales no se han consultado.</p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="inline-flex items-center gap-1.5 rounded-lg border border-[#dce2d9] bg-[#f8f9f5] px-2.5 py-2 font-mono text-[8px] text-[#829084]"><SlidersHorizontal size={12} /> SIN CARGAR</span>
                  <button type="button" onClick={() => setNotice(true)} className="pliegue-focus rounded-lg bg-[#2b4433] px-3.5 py-2.5 text-[10px] font-semibold text-[#f2f5ee] hover:bg-[#36543f]">Guardar · vista previa</button>
                </div>
              </div>
              <div className="rounded-[18px] border border-[#dce2d9] bg-[#f8f9f5] p-4 sm:p-5">
                <div className="mb-4 flex items-start gap-2.5 rounded-xl border border-[#e1e7de] bg-[#f1f4ee] px-3.5 py-3 text-[9px] leading-[1.6] text-[#758276]">
                  <WandSparkles size={14} className="mt-0.5 shrink-0 text-[#71874b]" />
                  <span><strong className="text-[#506353]">Ajustes locales, sin persistencia.</strong> Puedes explorar controles visualmente; no se leen ni guardan valores reales desde este canvas.</span>
                </div>
                <div className="grid gap-x-5 gap-y-6 md:grid-cols-2 xl:grid-cols-3">
                  <div>
                    <h3 className="mb-3 flex items-center gap-2 text-[10px] font-semibold text-[#3f5544]"><span className="h-1.5 w-1.5 rounded-full bg-[#9bb957]" /> Subtítulos</h3>
                    <div className="grid gap-3">
                      {styleFields.slice(0, 7).map((field) => <Field key={field.name} field={field} />)}
                    </div>
                  </div>
                  <div>
                    <h3 className="mb-3 flex items-center gap-2 text-[10px] font-semibold text-[#3f5544]"><span className="h-1.5 w-1.5 rounded-full bg-[#9bb957]" /> Composición</h3>
                    <div className="grid gap-3">
                      {styleFields.slice(7, 11).map((field) => <Field key={field.name} field={field} />)}
                    </div>
                    <div className="mt-4 flex items-center gap-2 rounded-lg border border-dashed border-[#d6ded3] px-3 py-2.5 text-[9px] text-[#879388]"><Clock3 size={13} /> Composición aplicada al nuevo archivo</div>
                  </div>
                  <div>
                    <h3 className="mb-3 flex items-center gap-2 text-[10px] font-semibold text-[#3f5544]"><span className="h-1.5 w-1.5 rounded-full bg-[#9bb957]" /> Límites y costes</h3>
                    <div className="grid gap-3">
                      {styleFields.slice(11).map((field) => <Field key={field.name} field={field} />)}
                    </div>
                  </div>
                </div>
              </div>
            </section>

            <footer className="mt-2 flex flex-col justify-between gap-2 border-t border-[#dce2d9] py-5 font-mono text-[8px] tracking-[.1em] text-[#929d92] sm:flex-row">
              <span>PLIEGUE <span className="text-[#bdc6bb]">/</span> ENTORNO LOCAL</span>
              <span>LAS TARIFAS Y CREDENCIALES NO SE HAN VERIFICADO</span>
            </footer>
          </div>
        </main>
      </div>

      <nav aria-label="Secciones" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-[#d9e1d7] bg-[#f5f7f2]/95 px-1 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden">
        {navItems.map(({ id, label, icon: Icon }) => (
          <button key={id} type="button" onClick={() => navigate(id)} aria-label={label} aria-current={active === id ? 'page' : undefined} className={`pliegue-focus flex min-h-[58px] flex-col items-center justify-center gap-1 text-[8px] font-medium ${active === id ? 'text-[#38563d]' : 'text-[#889489]'}`}>
            <Icon size={16} strokeWidth={active === id ? 2.2 : 1.7} /><span className="max-w-full truncate px-1">{label}</span>
          </button>
        ))}
      </nav>
      {notice && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-[#1c2b21]/35 p-4 sm:items-center" onClick={() => setNotice(false)}>
          <div role="dialog" aria-modal="true" aria-labelledby="design-dialog-title" onClick={(event) => event.stopPropagation()} className="w-full max-w-[390px] rounded-[20px] border border-[#dce3d8] bg-[#f8f9f5] p-5 shadow-[0_24px_80px_rgba(32,45,37,.2)]">
            <div className="mb-4 grid h-10 w-10 place-items-center rounded-xl bg-[#e9eee6] text-[#59705d]"><LockKeyhole size={18} /></div>
            <h2 id="design-dialog-title" className="mb-2 text-[17px] font-semibold tracking-[-.04em] text-[#2a3c30]">Solo vista de diseño</h2>
            <p className="mb-5 text-[11px] leading-[1.7] text-[#718075]">Esta maqueta no está conectada al servicio local. No se leerán ni subirán vídeos y no se guardarán cambios ni iniciarán trabajos.</p>
            <button type="button" autoFocus onClick={() => setNotice(false)} className="pliegue-focus w-full rounded-[10px] bg-[#2b4433] px-4 py-3 text-[11px] font-semibold text-[#f4f6f0]">Entendido</button>
          </div>
        </div>
      )}
      <div className="sr-only" aria-live="polite">{consent || unknownConsent ? 'Autorizaciones de muestra modificadas; no se enviará ningún dato.' : ''}</div>
    </div>
  );
}
