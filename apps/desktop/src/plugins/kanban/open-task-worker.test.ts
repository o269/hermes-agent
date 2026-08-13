import { beforeEach, describe, expect, it, vi } from 'vitest'

import { registerPluginLocales } from '@/i18n/plugin-i18n'
import { notify } from '@/store/notifications'

import { KANBAN_LOCALES } from './i18n'
import { openKanbanWorkerSession } from './open-task-worker'

const { activeProfile, openSession, prewarmProfileBackend } = vi.hoisted(() => ({
  activeProfile: { get: vi.fn(() => 'default') },
  openSession: vi.fn(),
  prewarmProfileBackend: vi.fn()
}))

vi.mock('@/app/open-session', () => ({ openSession: (...args: unknown[]) => openSession(...args) }))
vi.mock('@/store/profile', () => ({
  $activeGatewayProfile: activeProfile,
  prewarmProfileBackend: (...args: unknown[]) => prewarmProfileBackend(...args)
}))
vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

describe('openKanbanWorkerSession', () => {
  const navigate = vi.fn()
  const request = vi.fn()

  beforeEach(() => {
    registerPluginLocales('kanban', KANBAN_LOCALES)
    openSession.mockReset()
    prewarmProfileBackend.mockReset()
    activeProfile.get.mockReturnValue('default')
    navigate.mockReset()
    request.mockReset()
    vi.mocked(notify).mockReset()
  })

  it('happy path: for_task openable → resume with profile → openSession', async () => {
    request.mockImplementation(async (method: string) => {
      if (method === 'session.for_task') {
        return { openable: true, profile: 'coder', session_id: 'sess-worker-1' }
      }

      if (method === 'session.resume') {
        return { session_id: 'sess-worker-1' }
      }

      throw new Error(`unexpected ${method}`)
    })

    const result = await openKanbanWorkerSession({
      boardSlug: 'fleet',
      navigate,
      request,
      taskId: 't_abc123'
    })

    expect(result).toBe('opened')
    expect(request).toHaveBeenCalledWith('session.for_task', { board: 'fleet', task_id: 't_abc123' })
    expect(prewarmProfileBackend).toHaveBeenCalledWith('coder')
    expect(request).toHaveBeenCalledWith('session.resume', {
      cols: 96,
      lazy: true,
      profile: 'coder',
      session_id: 'sess-worker-1',
      source: 'desktop'
    })
    expect(openSession).toHaveBeenCalledWith('sess-worker-1', navigate, 'stack')
  })

  it('null session_id → info notify, no resume, fallback', async () => {
    request.mockResolvedValue({
      profile: 'coder',
      reason: 'no worker session recorded for this card yet',
      session_id: null,
      task_status: 'running'
    })

    const result = await openKanbanWorkerSession({ navigate, request, taskId: 't_wait' })

    expect(result).toBe('fallback')
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'info', message: expect.stringContaining('no worker session') })
    )
    expect(request.mock.calls.some(([method]) => method === 'session.resume')).toBe(false)
    expect(openSession).not.toHaveBeenCalled()
  })

  it('openable false → warning notify, no resume, fallback', async () => {
    request.mockResolvedValue({
      openable: false,
      profile: 'remote',
      session_id: 'sess-remote',
      task_status: 'running'
    })

    const result = await openKanbanWorkerSession({ navigate, request, taskId: 't_remote' })

    expect(result).toBe('fallback')
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'warning' }))
    expect(request.mock.calls.some(([method]) => method === 'session.resume')).toBe(false)
    expect(openSession).not.toHaveBeenCalled()
  })

  it('ignores a second concurrent open for the same task', async () => {
    let releaseResume!: () => void
    const resumeGate = new Promise<void>(resolve => {
      releaseResume = resolve
    })

    request.mockImplementation(async (method: string) => {
      if (method === 'session.for_task') {
        return { openable: true, profile: 'default', session_id: 'sess-dup' }
      }

      if (method === 'session.resume') {
        await resumeGate

        return { session_id: 'sess-dup' }
      }

      throw new Error(`unexpected ${method}`)
    })

    const first = openKanbanWorkerSession({ navigate, request, taskId: 't_dup' })
    const second = openKanbanWorkerSession({ navigate, request, taskId: 't_dup' })

    const secondResult = await second
    expect(secondResult).toBe('ignored')

    releaseResume()
    const firstResult = await first
    expect(firstResult).toBe('opened')
    expect(request.mock.calls.filter(([method]) => method === 'session.for_task').length).toBe(1)
  })

  it('resume error → error notify, fallback', async () => {
    request.mockImplementation(async (method: string) => {
      if (method === 'session.for_task') {
        return { openable: true, profile: 'coder', session_id: 'sess-boom' }
      }

      if (method === 'session.resume') {
        throw new Error('gateway wedged')
      }

      throw new Error(`unexpected ${method}`)
    })

    const result = await openKanbanWorkerSession({ navigate, request, taskId: 't_err' })

    expect(result).toBe('fallback')
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'error', message: expect.stringContaining('gateway wedged') })
    )
    expect(openSession).not.toHaveBeenCalled()
  })
})
