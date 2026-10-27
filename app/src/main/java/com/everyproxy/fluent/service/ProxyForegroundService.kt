package com.everyproxy.fluent.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import com.everyproxy.fluent.proxy.HttpProxyServer
import com.everyproxy.fluent.ui.MainActivity
import com.everyproxy.fluent.util.NetworkUtils
import java.util.Timer
import java.util.TimerTask

class ProxyForegroundService : Service() {

    companion object {
        const val CHANNEL_ID = "every_proxy_fluent_channel"
        const val NOTIFICATION_ID = 1001
        const val ACTION_START = "com.everyproxy.fluent.ACTION_START"
        const val ACTION_STOP = "com.everyproxy.fluent.ACTION_STOP"
        const val ACTION_STATUS_BROADCAST = "com.everyproxy.fluent.ACTION_STATUS"
        const val EXTRA_PORT = "EXTRA_PORT"

        var isServiceRunning = false
            private set

        var activePort = 8080
            private set

        var activeIp = "192.168.43.1"
            private set

        var serverInstance: HttpProxyServer? = null
            private set
    }

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var updateTimer: Timer? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        acquireLocks()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: ACTION_START

        if (action == ACTION_STOP) {
            stopProxy()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }

        val port = intent?.getIntExtra(EXTRA_PORT, 8080) ?: 8080
        startProxy(port)

        // START_STICKY гарантирует перезапуск сервиса системой Android при нехватке памяти
        return START_STICKY
    }

    private fun acquireLocks() {
        try {
            // Удерживаем процессор от глубокого сна (Doze mode) при выключенном экране
            val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = powerManager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "EveryProxyFluent::CpuWakeLock"
            ).apply {
                setReferenceCounted(false)
                acquire(24 * 60 * 60 * 1000L) // 24 часа
            }

            // Удерживаем Wi-Fi модуль от перехода в режим энергосбережения
            val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            wifiLock = wifiManager.createWifiLock(
                WifiManager.WIFI_MODE_FULL_HIGH_PERF,
                "EveryProxyFluent::WifiLock"
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {}
    }

    private fun releaseLocks() {
        try {
            if (wakeLock?.isHeld == true) {
                wakeLock?.release()
            }
            wakeLock = null

            if (wifiLock?.isHeld == true) {
                wifiLock?.release()
            }
            wifiLock = null
        } catch (_: Exception) {}
    }

    private fun startProxy(port: Int) {
        if (isServiceRunning && activePort == port) return

        activePort = port
        activeIp = NetworkUtils.getLocalIpAddress()

        // Останавливаем предыдущий экземпляр если был
        serverInstance?.stop()

        serverInstance = HttpProxyServer(port = port) { level, msg ->
            broadcastLog(level, msg)
        }
        serverInstance?.start()

        isServiceRunning = true

        // Запуск сервиса на переднем плане с постоянным уведомлением
        startForeground(NOTIFICATION_ID, buildNotification("Активен: $activeIp:$port"))

        broadcastState(true)

        // Периодическое обновление счетчика переданных данных в уведомлении
        updateTimer?.cancel()
        updateTimer = Timer().apply {
            scheduleAtFixedRate(object : TimerTask() {
                override fun run() {
                    if (isServiceRunning) {
                        val stats = serverInstance?.stats
                        val tx = stats?.formatBytes(stats.bytesSent.get()) ?: "0 B"
                        val rx = stats?.formatBytes(stats.bytesReceived.get()) ?: "0 B"
                        val subText = "Вх: $rx • Исх: $tx • Подкл: ${stats?.activeConnections?.get() ?: 0}"
                        val notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
                        notificationManager.notify(NOTIFICATION_ID, buildNotification("$activeIp:$port ($subText)"))
                    }
                }
            }, 3000L, 3000L)
        }
    }

    private fun stopProxy() {
        updateTimer?.cancel()
        updateTimer = null

        serverInstance?.stop()
        serverInstance = null
        isServiceRunning = false

        broadcastState(false)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "EveryProxy Fluent Service",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Постоянное уведомление активного мобильного прокси"
                setShowBadge(false)
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(contentText: String): Notification {
        val openAppIntent = Intent(this, MainActivity::class.java)
        val openAppPendingIntent = PendingIntent.getActivity(
            this,
            0,
            openAppIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val stopIntent = Intent(this, ProxyForegroundService::class.java).apply {
            action = ACTION_STOP
        }
        val stopPendingIntent = PendingIntent.getService(
            this,
            1,
            stopIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("🛡️ EveryProxy Fluent (HTTP)")
            .setContentText(contentText)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .setContentIntent(openAppPendingIntent)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Остановить", stopPendingIntent)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }

    private fun broadcastState(running: Boolean) {
        val intent = Intent(ACTION_STATUS_BROADCAST).apply {
            putExtra("is_running", running)
            putExtra("ip", activeIp)
            putExtra("port", activePort)
        }
        sendBroadcast(intent)
    }

    private fun broadcastLog(level: String, msg: String) {
        val intent = Intent(ACTION_STATUS_BROADCAST).apply {
            putExtra("log_level", level)
            putExtra("log_message", msg)
        }
        sendBroadcast(intent)
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        // Гарантирует, что если пользователь смахнул приложение из списка недавних,
        // прокси-сервер НЕ отключится и продолжит работать!
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        stopProxy()
        releaseLocks()
        super.onDestroy()
    }
}
