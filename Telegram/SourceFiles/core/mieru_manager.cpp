/*
This file is part of Telegram Desktop,
the official desktop application for the Telegram messaging service.

For license and copyright information please follow this link:
https://github.com/telegramdesktop/tdesktop/blob/master/LEGAL
*/
#include "core/mieru_manager.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QProcess>
#include <QtCore/QProcessEnvironment>
#include <QtCore/QSaveFile>
#include <QtCore/QStandardPaths>
#include <QtCore/QTimer>
#include <QtNetwork/QHostAddress>
#include <QtNetwork/QTcpServer>
#include <QtNetwork/QTcpSocket>

#include <gsl/util>

#ifdef Q_OS_WIN
#include <QtCore/qt_windows.h>
#endif // Q_OS_WIN

namespace Core {
namespace {

constexpr auto kStartupTimeout = 10 * 1000;
constexpr auto kReadyPollInterval = 100;
constexpr auto kRestartDelay = 1000;

[[nodiscard]] QJsonArray PortBindings(const MTP::ProxyData &proxy) {
	auto result = QJsonArray();
	const auto add = [&](const QString &protocol) {
		auto binding = QJsonObject{
			{ u"protocol"_q, protocol },
		};
		if (proxy.mieruPortRange.isEmpty()) {
			binding.insert(u"port"_q, int(proxy.port));
		} else {
			binding.insert(u"portRange"_q, proxy.mieruPortRange);
		}
		result.push_back(binding);
	};
	using Transport = MTP::ProxyData::MieruTransport;
	if (proxy.mieruTransport != Transport::Udp) {
		add(u"TCP"_q);
	}
	if (proxy.mieruTransport != Transport::Tcp) {
		add(u"UDP"_q);
	}
	return result;
}

[[nodiscard]] QJsonObject ServerConfig(const MTP::ProxyData &proxy) {
	auto result = QJsonObject{
		{ u"portBindings"_q, PortBindings(proxy) },
	};
	auto address = QHostAddress();
	if (address.setAddress(proxy.host)) {
		result.insert(u"ipAddress"_q, proxy.host);
	} else {
		result.insert(u"domainName"_q, proxy.host);
	}
	return result;
}

[[nodiscard]] QJsonDocument ClientConfig(
		const MTP::ProxyData &proxy,
		uint16 localPort) {
	const auto profile = QJsonObject{
		{ u"profileName"_q, u"telegramita"_q },
		{ u"user"_q, QJsonObject{
			{ u"name"_q, proxy.user },
			{ u"password"_q, proxy.password },
		} },
		{ u"servers"_q, QJsonArray{ ServerConfig(proxy) } },
		{ u"mtu"_q, 1400 },
	};
	return QJsonDocument(QJsonObject{
		{ u"profiles"_q, QJsonArray{ profile } },
		{ u"activeProfile"_q, u"telegramita"_q },
		{ u"rpcPort"_q, 0 },
		{ u"socks5Port"_q, int(localPort) },
		{ u"socks5ListenLAN"_q, false },
		{ u"advancedSettings"_q, QJsonObject{
			{ u"noCheckUpdate"_q, true },
		} },
		{ u"loggingLevel"_q, u"INFO"_q },
	});
}

} // namespace

class MieruJob {
public:
	MieruJob();
	~MieruJob();

	[[nodiscard]] bool assign(qint64 processId);

private:
#ifdef Q_OS_WIN
	HANDLE _handle = nullptr;
#endif // Q_OS_WIN

};

MieruJob::MieruJob() {
#ifdef Q_OS_WIN
	_handle = CreateJobObject(nullptr, nullptr);
	if (!_handle) {
		return;
	}
	auto limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
	limits.BasicLimitInformation.LimitFlags
		= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
	if (!SetInformationJobObject(
			_handle,
			JobObjectExtendedLimitInformation,
			&limits,
			sizeof(limits))) {
		CloseHandle(_handle);
		_handle = nullptr;
	}
#endif // Q_OS_WIN
}

MieruJob::~MieruJob() {
#ifdef Q_OS_WIN
	if (_handle) {
		CloseHandle(_handle);
	}
#endif // Q_OS_WIN
}

bool MieruJob::assign(qint64 processId) {
	if (processId <= 0) {
		return false;
	}
#ifdef Q_OS_WIN
	if (!_handle) {
		return false;
	}
	const auto process = OpenProcess(
		PROCESS_SET_QUOTA | PROCESS_TERMINATE,
		FALSE,
		DWORD(processId));
	if (!process) {
		return false;
	}
	const auto guard = gsl::finally([&] { CloseHandle(process); });
	if (!AssignProcessToJobObject(_handle, process)) {
		LOG(("Mieru Error: Could not assign child process to job (%1)."
			).arg(GetLastError()));
		return false;
	}
	return true;
#else // Q_OS_WIN
	return true;
#endif // Q_OS_WIN
}

MieruManager::MieruManager(Fn<void()> changed)
: _changed(std::move(changed))
, _restartTimer(std::make_unique<QTimer>())
, _startupTimer(std::make_unique<QTimer>())
, _job(std::make_unique<MieruJob>()) {
	_restartTimer->setSingleShot(true);
	_startupTimer->setSingleShot(true);
	connect(_restartTimer.get(), &QTimer::timeout, this, [=] { launch(); });
	connect(_startupTimer.get(), &QTimer::timeout, this, [=] {
		scheduleRestart(u"Timed out waiting for local SOCKS5 endpoint."_q);
	});
}

MieruManager::~MieruManager() {
	stopProcess(true);
}

void MieruManager::setProxy(
		const MTP::ProxyData &proxy,
		bool enabled) {
	const auto desired = (enabled
		&& proxy.type == MTP::ProxyData::Type::Mieru)
		? proxy
		: MTP::ProxyData();
	if (_desired == desired
		&& (_state == State::Starting || _state == State::Ready)) {
		return;
	}
	++_generation;
	stopProcess(false);
	_desired = desired;
	if (_desired.type != MTP::ProxyData::Type::Mieru) {
		setState(State::Disabled);
		return;
	}
	launch();
}

MTP::ProxyData MieruManager::proxy() const {
	if (_desired.type != MTP::ProxyData::Type::Mieru) {
		return MTP::ProxyData();
	}
	return MTP::ProxyData{
		MTP::ProxyData::Type::Socks5,
		u"127.0.0.1"_q,
		(_localPort ? _localPort : uint16(1)),
		QString(),
		QString(),
	};
}

MieruManager::State MieruManager::state() const {
	return _state;
}

QString MieruManager::lastError() const {
	return _lastError;
}

void MieruManager::launch() {
	if (_desired.type != MTP::ProxyData::Type::Mieru) {
		return;
	}
	if (!_desired.valid()) {
		setState(State::Failed, u"The Mieru configuration is invalid."_q);
		return;
	}
	const auto executable = executablePath();
	if (executable.isEmpty()) {
		setState(State::Failed, u"mieru.exe was not found next to Telegram.exe."_q);
		return;
	}
	_localPort = chooseLocalPort();
	if (!_localPort) {
		scheduleRestart(u"Could not allocate a local SOCKS5 port."_q);
		return;
	}
	if (!writeConfig()) {
		scheduleRestart(u"Could not write the Mieru client configuration."_q);
		return;
	}

	_process = std::make_unique<QProcess>();
	_process->setProgram(executable);
	_process->setArguments({ u"run"_q });
	_process->setProcessChannelMode(QProcess::MergedChannels);
	auto environment = QProcessEnvironment::systemEnvironment();
	environment.insert(u"MIERU_CONFIG_JSON_FILE"_q, _configPath);
	_process->setProcessEnvironment(environment);
	connect(_process.get(), &QProcess::readyRead, this, [=] {
		const auto output = QString::fromUtf8(
			_process->readAll()).trimmed();
		if (!output.isEmpty()) {
			LOG(("Mieru Info: %1").arg(output));
		}
	});
	connect(
		_process.get(),
		qOverload<int, QProcess::ExitStatus>(&QProcess::finished),
		this,
		[=, generation = _generation](int exitCode) {
			handleProcessFinished(exitCode, generation);
		});
	setState(State::Starting);
	_process->start();
	if (!_process->waitForStarted(5000)) {
		scheduleRestart(u"Could not start mieru.exe: "_q
			+ _process->errorString());
		return;
	}
	if (!_job->assign(_process->processId())) {
		stopProcess(false);
		setState(
			State::Failed,
			u"Could not attach mieru.exe to the process job."_q);
		return;
	}
	_startupTimer->start(kStartupTimeout);
	checkReady();
}

void MieruManager::stopProcess(bool clearDesired) {
	_restartTimer->stop();
	_startupTimer->stop();
	_readySocket = nullptr;
	_stopping = true;
	if (_process) {
		disconnect(_process.get(), nullptr, this, nullptr);
		if (_process->state() != QProcess::NotRunning) {
			_process->terminate();
			if (!_process->waitForFinished(1500)) {
				_process->kill();
				_process->waitForFinished(1500);
			}
		}
		_process = nullptr;
	}
	_stopping = false;
	_localPort = 0;
	if (!_configPath.isEmpty()) {
		QFile::remove(_configPath);
		_configPath = QString();
	}
	if (clearDesired) {
		_desired = MTP::ProxyData();
	}
}

void MieruManager::handleProcessFinished(int exitCode, uint64 generation) {
	if (_stopping
		|| generation != _generation
		|| _desired.type != MTP::ProxyData::Type::Mieru) {
		return;
	}
	QTimer::singleShot(0, this, [=] {
		if (generation != _generation) {
			return;
		}
		scheduleRestart(u"mieru.exe exited unexpectedly with code "_q
			+ QString::number(exitCode) + '.');
	});
}

void MieruManager::scheduleRestart(QString error) {
	LOG(("Mieru Error: %1").arg(error));
	stopProcess(false);
	setState(State::Failed, std::move(error));
	if (_desired.type == MTP::ProxyData::Type::Mieru) {
		_restartTimer->start(kRestartDelay);
	}
}

void MieruManager::checkReady() {
	if (_readySocket
		|| !_process
		|| _process->state() != QProcess::Running) {
		return;
	}
	_readySocket = std::make_unique<QTcpSocket>();
	const auto socket = _readySocket.get();
	connect(socket, &QTcpSocket::connected, this, [=] {
		if (socket != _readySocket.get()) {
			return;
		}
		const auto greeting = QByteArray::fromRawData("\x05\x01\x00", 3);
		socket->write(greeting);
	});
	connect(socket, &QTcpSocket::readyRead, this, [=] {
		if (socket != _readySocket.get() || socket->bytesAvailable() < 2) {
			return;
		}
		const auto reply = socket->read(2);
		if (reply != QByteArray::fromRawData("\x05\x00", 2)) {
			_readySocket.release()->deleteLater();
			QTimer::singleShot(kReadyPollInterval, this, [=] { checkReady(); });
			return;
		}
		_startupTimer->stop();
		_readySocket.release()->deleteLater();
		setState(State::Ready);
	});
	connect(socket, &QTcpSocket::errorOccurred, this, [=] {
		if (socket != _readySocket.get()) {
			return;
		}
		_readySocket.release()->deleteLater();
		QTimer::singleShot(kReadyPollInterval, this, [=] { checkReady(); });
	});
	socket->connectToHost(QHostAddress::LocalHost, _localPort);
}

void MieruManager::setState(State state, QString error) {
	if (_state == state && _lastError == error) {
		return;
	}
	_state = state;
	_lastError = std::move(error);
	_changed();
}

bool MieruManager::writeConfig() {
	const auto root = QStandardPaths::writableLocation(
		QStandardPaths::AppLocalDataLocation);
	const auto directory = QDir(root).filePath(u"mieru"_q);
	if (root.isEmpty() || !QDir().mkpath(directory)) {
		return false;
	}
	_configPath = QDir(directory).filePath(u"telegramita-client-%1.json"_q
		.arg(QCoreApplication::applicationPid()));
	auto file = QSaveFile(_configPath);
	if (!file.open(QIODevice::WriteOnly)) {
		return false;
	}
	if (file.write(ClientConfig(_desired, _localPort).toJson(
			QJsonDocument::Compact)) < 0) {
		return false;
	}
	return file.commit();
}

QString MieruManager::executablePath() const {
	const auto path = QDir(
		QCoreApplication::applicationDirPath()).filePath(u"mieru.exe"_q);
	return QFile::exists(path) ? path : QString();
}

uint16 MieruManager::chooseLocalPort() const {
	auto server = QTcpServer();
	if (!server.listen(QHostAddress::LocalHost, 0)) {
		return 0;
	}
	return uint16(server.serverPort());
}

} // namespace Core
