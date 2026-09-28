import type { MouseEvent, ReactNode } from 'react'
import { navigate } from '../route'

type Props = { to: string; className?: string; children: ReactNode; title?: string; 'aria-label'?: string;
  'aria-current'?: 'page' | undefined }

/** In-app link: renders a real href (middle-click / open in new tab still work) but a plain
 *  click goes through the router instead of the browser following a raw anchor. */
export default function Link({ to, className, children, ...rest }: Props) {
  const href = to.startsWith('#') ? to : `#${to}`
  function onClick(e: MouseEvent<HTMLAnchorElement>) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    navigate(href)
  }
  return <a href={href} className={className} onClick={onClick} {...rest}>{children}</a>
}
