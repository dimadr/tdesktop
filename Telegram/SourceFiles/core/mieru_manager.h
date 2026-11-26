/*
This file is part of Telegram Desktop,
the official desktop application for the Telegram messaging service.

For license and copyright information please follow this link:
https://github.com/telegramdesktop/tdesktop/blob/master/LEGAL
*/
#pragma once

#include "base/basic_types.h"
#include "mtproto/mtproto_proxy_data.h"

#include <QtCore/QObject>

#include <memory>

class QProcess;
class QTcpSocket;
class QTimer;

namespace Core {

class MieruJob;

class MieruManager final : public QObject {
public:
	enum class State {
		Disabled,
		Starting,
		Ready,
		Failed,
	};

	explicit MieruManager(Fn<void()> changed);
	~MieruManager();

	void setProxy(const MTP::ProxyData &proxy, bool enabled);
	[[nodiscard]] MTP::ProxyData proxy() const;
	[[nodiscard]] State state() const;
	[[nodiscard]] QString lastError() const;

private:
	void launch();
	void stopProcess(bool clearDesired);
	void handleProcessFinished(int exitCode, uint64 generation);
	void scheduleRestart(QString error);
	void checkReady();
	void setState(State state, QString error = QString());
	[[nodiscard]] bool writeConfig();
	[[nodiscard]] QString executablePath() const;
	[[nodiscard]] uint16 chooseLocalPort() const;

	Fn<void()> _changed;
	MTP::ProxyData _desired;
	std::unique_ptr<QProcess> _process;
	std::unique_ptr<QTcpSocket> _readySocket;
	std::unique_ptr<QTimer> _restartTimer;
	std::unique_ptr<QTimer> _startupTimer;
	std::unique_ptr<MieruJob> _job;
	QString _configPath;
	QString _lastError;
	uint16 _localPort = 0;
	uint64 _generation = 0;
	State _state = State::Disabled;
	bool _stopping = false;

};

} // namespace Core
