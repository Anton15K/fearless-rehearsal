import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Crosshair, Lock, ShieldBan, X } from 'lucide-react'

const championIds: Record<string, string> = {
  'Bel\'Veth': 'Belveth', 'Cho\'Gath': 'Chogath', 'Kai\'Sa': 'Kaisa',
  'Kha\'Zix': 'Khazix', 'Kog\'Maw': 'KogMaw', 'K\'Sante': 'KSante',
  'LeBlanc': 'Leblanc', 'Nunu & Willump': 'Nunu', 'Renata Glasc': 'Renata',
  'Vel\'Koz': 'Velkoz', 'Wukong': 'MonkeyKing', 'Rek\'Sai': 'RekSai',
}

export function Portrait({ name, size = 36, muted = false }: { name: string; size?: number; muted?: boolean }) {
  const [failed, setFailed] = useState(false)
  const cleanName = name.replaceAll('’', "'")
  const id = championIds[cleanName] ?? cleanName.replace(/[^A-Za-z0-9]/g, '')
  return (
    <span className={`portrait ${muted ? 'portrait-muted' : ''}`} style={{ width: size, height: size }} aria-hidden="true">
      {failed ? name.slice(0, 2).toUpperCase() :
        <img src={`https://ddragon.leagueoflegends.com/cdn/16.23.1/img/champion/${id}.png`} alt="" decoding="async" onError={() => setFailed(true)} />}
    </span>
  )
}

export function ActionIcon({ kind, size = 15 }: { kind: string; size?: number }) {
  return kind === 'ban' ? <ShieldBan size={size} /> : <Crosshair size={size} />
}

export function Dialog({ title, description, children, close }: {
  title: string; description?: string; children: ReactNode; close: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    ref.current?.showModal()
  }, [])
  return (
    <dialog ref={ref} onCancel={close} aria-labelledby="dialog-title" className="dialog">
      <div className="dialog-heading">
        <h2 id="dialog-title">{title}</h2>
        <button type="button" className="icon-button" aria-label="Close dialog" onClick={close}><X size={19} /></button>
      </div>
      {description && <p className="muted dialog-description">{description}</p>}
      {children}
    </dialog>
  )
}

export function LockCount({ count }: { count: number }) {
  return <span className="lock-count"><Lock size={12} /> {count} locked</span>
}
