package com.tethervpn.ui

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.tethervpn.R
import com.tethervpn.client.ProxyChecker
import com.tethervpn.client.ProxyClientVpnService
import com.tethervpn.databinding.ActivityMainBinding
import com.tethervpn.proxy.ProxyStats
import com.tethervpn.service.ProxyForegroundService
import com.tethervpn.util.NetworkUtils
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val prefs by lazy { getSharedPreferences("tethervpn_prefs", Context.MODE_PRIVATE) }

    private val requestNotificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ -> }

    private var pendingClientHost: String? = null
    private var pendingClientPort: Int = 8080

    private val vpnPrepareLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK) {
            val host = pendingClientHost ?: "192.168.43.1"
            ProxyClientVpnService.startClient(this, host, pendingClientPort)
        } else {
            Toast.makeText(this, "Разрешение на VPN отклонено", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        checkPermissions()
        setupModeTabs()
        setupServerViews()
        setupClientViews()
        observeServerState()
        observeClientState()
        startStatsUpdateLoop()
    }

    override fun onResume() {
        super.onResume()
        updateServerIp()
    }

    private fun checkPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                requestNotificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
    }

    private fun setupModeTabs() {
        binding.btnTabShare.setOnClickListener {
            switchMode(isShareMode = true)
        }
        binding.btnTabConnect.setOnClickListener {
            switchMode(isShareMode = false)
        }
    }

    private fun switchMode(isShareMode: Boolean) {
        if (isShareMode) {
            binding.btnTabShare.setBackgroundResource(R.drawable.bg_tab_active)
            binding.btnTabShare.setTextColor(ContextCompat.getColor(this, R.color.text_white))

            binding.btnTabConnect.setBackgroundResource(R.drawable.bg_tab_inactive)
            binding.btnTabConnect.setTextColor(ContextCompat.getColor(this, R.color.text_muted))

            binding.layoutShare.visibility = View.VISIBLE
            binding.layoutConnect.visibility = View.GONE
            updateServerIp()
        } else {
            binding.btnTabConnect.setBackgroundResource(R.drawable.bg_tab_active)
            binding.btnTabConnect.setTextColor(ContextCompat.getColor(this, R.color.text_white))

            binding.btnTabShare.setBackgroundResource(R.drawable.bg_tab_inactive)
            binding.btnTabShare.setTextColor(ContextCompat.getColor(this, R.color.text_muted))

            binding.layoutShare.visibility = View.GONE
            binding.layoutConnect.visibility = View.VISIBLE

            // Auto-detect gateway if target IP is not filled
            val currentTarget = binding.etTargetIp.text.toString().trim()
            if (currentTarget.isEmpty() || currentTarget == "192.168.43.1") {
                val gw = NetworkUtils.getWifiGatewayIp(this)
                binding.etTargetIp.setText(gw)
            }
        }
    }

    // =====================================================================
    // РЕЖИМ 1: РАЗДАЧА (СЕРВЕР)
    // =====================================================================
    private fun setupServerViews() {
        val savedPort = prefs.getInt("server_port", 8080)
        binding.etServerPort.setText(savedPort.toString())

        binding.btnRefreshServerIp.setOnClickListener {
            updateServerIp()
            Toast.makeText(this, "IP-адрес обновлен", Toast.LENGTH_SHORT).show()
        }

        binding.btnCopyServerAddress.setOnClickListener {
            val ip = binding.tvServerIp.text.toString()
            val port = binding.etServerPort.text.toString().trim()
            val formatted = "$ip:$port"

            val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            val clip = ClipData.newPlainText("TetherVPN Proxy", formatted)
            clipboard.setPrimaryClip(clip)

            Toast.makeText(this, getString(R.string.copied_toast) + " ($formatted)", Toast.LENGTH_SHORT).show()
        }

        binding.btnToggleServer.setOnClickListener {
            if (ProxyForegroundService.isRunningFlow.value) {
                ProxyForegroundService.stopService(this)
            } else {
                val portStr = binding.etServerPort.text.toString().trim()
                val port = portStr.toIntOrNull() ?: 8080
                if (port in 1024..65535) {
                    prefs.edit().putInt("server_port", port).apply()
                    ProxyForegroundService.startService(this, port)
                } else {
                    Toast.makeText(this, "Введите корректный порт (1024-65535)", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun updateServerIp() {
        val ip = NetworkUtils.getPrimaryIp()
        binding.tvServerIp.text = ip
    }

    private fun observeServerState() {
        lifecycleScope.launch {
            ProxyForegroundService.isRunningFlow.collect { running ->
                updateServerUi(running)
            }
        }
    }

    private fun updateServerUi(running: Boolean) {
        if (running) {
            binding.tvServerStatusBadge.text = getString(R.string.status_running)
            binding.tvServerStatusBadge.setTextColor(ContextCompat.getColor(this, R.color.accent_green))
            binding.tvServerStatusBadge.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_green_bg)
            )

            binding.btnToggleServer.text = getString(R.string.btn_stop_server)
            binding.btnToggleServer.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_red)
            )
            binding.tvServerHint.text = getString(R.string.server_toggle_hint_active)

            binding.etServerPort.isEnabled = false
            binding.cardServerStats.visibility = View.VISIBLE
        } else {
            binding.tvServerStatusBadge.text = getString(R.string.status_stopped)
            binding.tvServerStatusBadge.setTextColor(ContextCompat.getColor(this, R.color.accent_red))
            binding.tvServerStatusBadge.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_red_bg)
            )

            binding.btnToggleServer.text = getString(R.string.btn_start_server)
            binding.btnToggleServer.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_blue)
            )
            binding.tvServerHint.text = getString(R.string.server_toggle_hint_idle)

            binding.etServerPort.isEnabled = true
            binding.cardServerStats.visibility = View.GONE
        }
    }

    private fun startStatsUpdateLoop() {
        lifecycleScope.launch {
            while (isActive) {
                if (ProxyForegroundService.isRunningFlow.value) {
                    val activeConn = ProxyStats.activeConnections.get()
                    val sent = ProxyStats.formatBytes(ProxyStats.bytesSent.get())
                    val received = ProxyStats.formatBytes(ProxyStats.bytesReceived.get())

                    binding.tvActiveConnections.text = getString(R.string.stats_active_connections, activeConn)
                    binding.tvTrafficStats.text = getString(R.string.stats_traffic, sent, received)
                }
                delay(1000)
            }
        }
    }

    // =====================================================================
    // РЕЖИМ 2: ПОДКЛЮЧЕНИЕ (КЛИЕНТ)
    // =====================================================================
    private fun setupClientViews() {
        val savedTargetIp = prefs.getString("client_target_ip", NetworkUtils.getWifiGatewayIp(this))
        val savedTargetPort = prefs.getInt("client_target_port", 8080)

        binding.etTargetIp.setText(savedTargetIp)
        binding.etTargetPort.setText(savedTargetPort.toString())

        // Auto detect gateway
        binding.btnDetectGateway.setOnClickListener {
            val gw = NetworkUtils.getWifiGatewayIp(this)
            binding.etTargetIp.setText(gw)
            Toast.makeText(this, "Определен шлюз Wi-Fi: $gw", Toast.LENGTH_SHORT).show()
        }

        // Check proxy availability
        binding.btnCheckProxy.setOnClickListener {
            val host = binding.etTargetIp.text.toString().trim()
            val port = binding.etTargetPort.text.toString().trim().toIntOrNull() ?: 8080

            if (host.isEmpty()) {
                binding.tvClientCheckResult.text = "Укажите IP-адрес сервера"
                return@setOnClickListener
            }

            binding.tvClientCheckResult.text = getString(R.string.client_status_checking)
            binding.tvClientCheckResult.setTextColor(ContextCompat.getColor(this, R.color.accent_yellow))

            lifecycleScope.launch {
                when (val result = ProxyChecker.checkProxy(host, port)) {
                    is ProxyChecker.CheckResult.Success -> {
                        binding.tvClientCheckResult.text = getString(R.string.client_status_success, result.latencyMs)
                        binding.tvClientCheckResult.setTextColor(ContextCompat.getColor(this@MainActivity, R.color.accent_green))
                    }
                    is ProxyChecker.CheckResult.Error -> {
                        binding.tvClientCheckResult.text = getString(R.string.client_status_fail, result.message)
                        binding.tvClientCheckResult.setTextColor(ContextCompat.getColor(this@MainActivity, R.color.accent_red))
                    }
                }
            }
        }

        // Toggle Client Connection
        binding.btnToggleClient.setOnClickListener {
            if (ProxyClientVpnService.isClientConnectedFlow.value) {
                ProxyClientVpnService.stopClient(this)
            } else {
                val host = binding.etTargetIp.text.toString().trim()
                val port = binding.etTargetPort.text.toString().trim().toIntOrNull() ?: 8080

                if (host.isEmpty()) {
                    Toast.makeText(this, "Введите IP-адрес сервера раздачи", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }

                prefs.edit()
                    .putString("client_target_ip", host)
                    .putInt("client_target_port", port)
                    .apply()

                pendingClientHost = host
                pendingClientPort = port

                // Prepare VPN service
                val intent = VpnService.prepare(this)
                if (intent != null) {
                    vpnPrepareLauncher.launch(intent)
                } else {
                    ProxyClientVpnService.startClient(this, host, port)
                }
            }
        }

        // Open Wi-Fi settings
        binding.btnOpenWifiSettings.setOnClickListener {
            try {
                startActivity(Intent(Settings.ACTION_WIFI_SETTINGS))
            } catch (e: Exception) {
                Toast.makeText(this, "Откройте настройки Wi-Fi вручную", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun observeClientState() {
        lifecycleScope.launch {
            ProxyClientVpnService.isClientConnectedFlow.collect { connected ->
                updateClientUi(connected)
            }
        }
    }

    private fun updateClientUi(connected: Boolean) {
        if (connected) {
            binding.tvClientStatusBadge.text = getString(R.string.status_connected)
            binding.tvClientStatusBadge.setTextColor(ContextCompat.getColor(this, R.color.accent_green))
            binding.tvClientStatusBadge.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_green_bg)
            )

            binding.btnToggleClient.text = getString(R.string.btn_disconnect_vpn)
            binding.btnToggleClient.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_red)
            )
            binding.tvClientHint.text = "Трафик направляется через ${binding.etTargetIp.text}:${binding.etTargetPort.text}"

            binding.etTargetIp.isEnabled = false
            binding.etTargetPort.isEnabled = false
            binding.btnDetectGateway.isEnabled = false
        } else {
            binding.tvClientStatusBadge.text = getString(R.string.status_stopped)
            binding.tvClientStatusBadge.setTextColor(ContextCompat.getColor(this, R.color.accent_red))
            binding.tvClientStatusBadge.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_red_bg)
            )

            binding.btnToggleClient.text = getString(R.string.btn_connect_vpn)
            binding.btnToggleClient.backgroundTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this, R.color.accent_blue)
            )
            binding.tvClientHint.text = "Направляет весь трафик через прокси раздающего"

            binding.etTargetIp.isEnabled = true
            binding.etTargetPort.isEnabled = true
            binding.btnDetectGateway.isEnabled = true
        }
    }
}
