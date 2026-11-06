package com.tethervpn.client

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.ProxyInfo
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.tethervpn.R
import com.tethervpn.ui.MainActivity
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

class ProxyClientVpnService : VpnService() {

    companion object {
        const val ACTION_CONNECT = "com.tethervpn.CLIENT_CONNECT"
        const val ACTION_DISCONNECT = "com.tethervpn.CLIENT_DISCONNECT"
        const val EXTRA_HOST = "com.tethervpn.EXTRA_HOST"
        const val EXTRA_PORT = "com.tethervpn.EXTRA_PORT"

        private const val CHANNEL_ID = "tethervpn_client_channel"
        private const val NOTIFICATION_ID = 2002

        private val _isClientConnectedFlow = MutableStateFlow(false)
        val isClientConnectedFlow: StateFlow<Boolean> = _isClientConnectedFlow

        private val _connectedHostFlow = MutableStateFlow("")
        val connectedHostFlow: StateFlow<String> = _connectedHostFlow

        private val _connectedPortFlow = MutableStateFlow(8080)
        val connectedPortFlow: StateFlow<Int> = _connectedPortFlow

        fun startClient(context: Context, host: String, port: Int) {
            val intent = Intent(context, ProxyClientVpnService::class.java).apply {
                action = ACTION_CONNECT
                putExtra(EXTRA_HOST, host)
                putExtra(EXTRA_PORT, port)
            }
            ContextCompat.startForegroundService(context, intent)
        }

        fun stopClient(context: Context) {
            val intent = Intent(context, ProxyClientVpnService::class.java).apply {
                action = ACTION_DISCONNECT
            }
            context.startService(intent)
        }
    }

    private val tag = "TetherVPN:ClientVpn"
    private var vpnInterface: ParcelFileDescriptor? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: ACTION_CONNECT

        if (action == ACTION_DISCONNECT) {
            disconnectClient()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }

        val host = intent?.getStringExtra(EXTRA_HOST) ?: "192.168.43.1"
        val port = intent?.getIntExtra(EXTRA_PORT, 8080) ?: 8080

        connectClient(host, port)
        return START_STICKY
    }

    private fun connectClient(host: String, port: Int) {
        disconnectClient()

        try {
            acquireWakeLock()

            val notification = buildNotification(host, port)
            startForeground(NOTIFICATION_ID, notification)

            val builder = Builder()
                .setSession("TetherVPN Client")
                .setMtu(1500)
                .addAddress("10.254.1.1", 32)
                .addRoute("10.254.1.1", 32)

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val proxyInfo = ProxyInfo.buildDirectProxy(host, port)
                builder.setHttpProxy(proxyInfo)
            }

            vpnInterface = builder.establish()

            _connectedHostFlow.value = host
            _connectedPortFlow.value = port
            _isClientConnectedFlow.value = true
            Log.i(tag, "Client VPN connected via proxy $host:$port")
        } catch (e: Exception) {
            Log.e(tag, "Failed to establish VPN client: ${e.message}", e)
            disconnectClient()
            stopSelf()
        }
    }

    private fun disconnectClient() {
        try {
            vpnInterface?.close()
        } catch (ignored: Exception) {}
        vpnInterface = null

        releaseWakeLock()
        _isClientConnectedFlow.value = false
        Log.i(tag, "Client VPN disconnected")
    }

    private fun acquireWakeLock() {
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as? PowerManager
            wakeLock = pm?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TetherVPN:ClientWakeLock")?.apply {
                setReferenceCounted(false)
                acquire(12 * 60 * 60 * 1000L)
            }
        } catch (ignored: Exception) {}
    }

    private fun releaseWakeLock() {
        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
        } catch (ignored: Exception) {}
        wakeLock = null
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.notification_channel_desc)
                setShowBadge(false)
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(host: String, port: Int): Notification {
        val mainIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val mainPendingIntent = PendingIntent.getActivity(
            this,
            0,
            mainIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val disconnectIntent = Intent(this, ProxyClientVpnService::class.java).apply {
            action = ACTION_DISCONNECT
        }
        val disconnectPendingIntent = PendingIntent.getService(
            this,
            2,
            disconnectIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_client_title))
            .setContentText(getString(R.string.notification_client_content, host, port))
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentIntent(mainPendingIntent)
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                getString(R.string.btn_disconnect_vpn),
                disconnectPendingIntent
            )
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }

    override fun onDestroy() {
        disconnectClient()
        super.onDestroy()
    }
}
