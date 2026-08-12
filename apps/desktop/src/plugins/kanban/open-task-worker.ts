/**
 * Open a kanban card's executing worker session in the Desktop chat surface.
 * Backend contract: `session.for_task` → `session.resume` → `openSession`.
 */

import { notify } from '@/store/notifications'
import { openSession, type OpenSessionNavigate } from '@/app/open-session'
import { translatePlugin } from '@/i18n/plugin-i18n'
import { getRuntimeI18nLocale } from '@/i18n/runtime'
import { $activeGatewayProfile, prewarmProfileBackend } from '@/store/profile'

type GatewayRequest = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

interface ForTaskResponse {
  claim_lock?: string
  openable?: boolean
  profile?: null | string
  reason?: string
  session_id?: null | string
  task_status?: string
}

export type OpenKanbanWorkerResult = 'fallback' | 'ignored' | 'opened'

const inFlight = new Set<string>()

function t(key: string, ...args: unknown[]): string {
  return translatePlugin('kanban', getRuntimeI18nLocale(), key, args)
}

function normalizeProfileKey(name: string): string {
  const trimmed = name.trim()

  return trimmed || 'default'
}

export async function openKanbanWorkerSession({
  boardSlug,
  navigate,
  request,
  taskId
}: {
  boardSlug?: string
  navigate: OpenSessionNavigate
  request: GatewayRequest
  taskId: string
}): Promise<OpenKanbanWorkerResult> {
  const id = taskId.trim()

  if (!id) {
    return 'fallback'
  }

  if (inFlight.has(id)) {
    return 'ignored'
  }

  inFlight.add(id)

  try {
    const forTask = await request<ForTaskResponse>('session.for_task', {
      ...(boardSlug ? { board: boardSlug } : {}),
      task_id: id
    })

    if (!forTask.session_id) {
      notify({ kind: 'info', message: t('workerNotReady', forTask.reason ?? '') })

      return 'fallback'
    }

    const profile = (forTask.profile ?? '').trim()

    if (!forTask.openable) {
      notify({ kind: 'warning', message: t('workerNotOnHost') })

      return 'fallback'
    }

    if (profile && normalizeProfileKey(profile) !== normalizeProfileKey($activeGatewayProfile.get())) {
      prewarmProfileBackend(profile)
    }

    try {
      await request('session.resume', {
        cols: 96,
        lazy: true,
        session_id: forTask.session_id,
        source: 'desktop',
        ...(profile ? { profile } : {})
      })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      notify({ kind: 'error', message: t('workerOpenFailed', message) })

      return 'fallback'
    }

    openSession(forTask.session_id, navigate, 'stack')

    return 'opened'
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    notify({ kind: 'error', message: t('workerOpenFailed', message) })

    return 'fallback'
  } finally {
    inFlight.delete(id)
  }
}
